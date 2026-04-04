import asyncio
import hashlib
from typing import Optional, Union, List, Any
from fastapi import Depends, HTTPException, Request, status, WebSocket
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from db.supabase_client import get_supabase_client
from db.redis_client import get_cache, set_cache

bearer_scheme = HTTPBearer()

# ── User Access Control Check ─────────────────────────────────────────────────

async def _check_user_access(user_id: str, email: str) -> None:
    """
    Real-time access gate:
    1. Redis cache (50ms — avoids DB hit on every request)
    2. Supabase users table (ground truth — instant if user was deleted/banned)
    Raises 403 immediately if user is deleted or is_active=False.
    """
    cache_key = f"access:{user_id}"
    cached = await get_cache(cache_key)

    # Fast path: cache says allowed (TTL=30s to stay near-realtime)
    if cached == "ok":
        return

    # Slow path: check users table in Supabase
    try:
        sb = get_supabase_client()

        def _query():
            return (sb.table("users")
                      .select("is_active")
                      .eq("id", user_id)
                      .single()
                      .execute())
        result = await asyncio.to_thread(_query)
        row = result.data

    except Exception:
        # If DB is unreachable — fail open (don't block users on infra errors)
        return

    if not row:
        # User deleted from users table → access revoked immediately
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account not found or has been removed."
        )

    if not row.get("is_active", True):
        # User banned (is_active = false)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been suspended. Contact support."
        )

    # Access granted — cache for 30s (real-time enough: deletion takes effect < 30s)
    await set_cache(cache_key, "ok", expire=30)

async def _update_last_seen(user_id: str) -> None:
    """Update last_seen in the background (non-blocking)."""
    try:
        sb = get_supabase_client()
        from datetime import datetime, timezone

        def _update():
            sb.table("users").update({"last_seen": datetime.now(timezone.utc).isoformat()}).eq("id", user_id).execute()
        asyncio.create_task(asyncio.to_thread(_update))
    except Exception:
        pass  # Non-critical

# ── HTTP Bearer Auth ──────────────────────────────────────────────────────────

async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)
) -> dict:
    """
    FastAPI dependency: validates Supabase JWT + checks users access table.
    Raises 401 if token is invalid, 403 if user was deleted or banned.
    """
    token = credentials.credentials
    try:
        sb = get_supabase_client()
        response = sb.auth.get_user(token)
        if not response or not response.user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        user_id = str(response.user.id)
        email = response.user.email

        # Real-time access check against users table
        await _check_user_access(user_id, email)

        # Update last_seen (background, non-blocking)
        await _update_last_seen(user_id)

        return {"id": user_id, "email": email}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )

# ── WebSocket Auth ────────────────────────────────────────────────────────────

async def get_ws_user(websocket: WebSocket) -> Optional[dict]:
    """
    WebSocket auth: reads token from ?token=<jwt> query param.
    Also checks the users access table for real-time access control.
    """
    token = websocket.query_params.get("token")
    if not token:
        return None
    try:
        sb = get_supabase_client()
        response = sb.auth.get_user(token)
        if response and response.user:
            user_id = str(response.user.id)
            email = response.user.email

            # Real-time access check
            await _check_user_access(user_id, email)
            await _update_last_seen(user_id)

            return {"id": user_id, "email": email}
    except HTTPException:
        raise  # Let WS handler deal with 403
    except Exception as e:
        err_msg = str(e).lower()
        if "expired" in err_msg:
            print(f"⚠️ WS Auth: Token expired for connection request.")
        else:
            print(f"❌ WS Auth failed: {e}")
    return None
