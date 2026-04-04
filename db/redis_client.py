from datetime import datetime, timezone, timedelta
import os
import json
import asyncio
from typing import Optional, List, Union

# The user explicitly mandated replacing Redis completely with Supabase
_real_redis = None

def _get_sb():
    from db.supabase_client import get_supabase_client
    return get_supabase_client()

# ── Low-level safe get/set (Supabase Only) ────────────────────────────────────

async def get_cache(key: str):
    for attempt in range(2):
        try:
            def _get():
                sb = _get_sb()
                res = sb.table("app_cache").select("value, expires_at").eq("key", key).execute()
                if not res.data: return None
                row = res.data[0]
                
                if row.get("expires_at"):
                    raw_exp = row["expires_at"].replace("Z", "+00:00")
                    try:
                        exp = datetime.fromisoformat(raw_exp)
                    except ValueError:
                        if "." in raw_exp:
                            base, rest = raw_exp.split(".")
                            frac = rest.split("+")[0].split("-")[0]
                            tz = rest[len(frac):]
                            frac = (frac + "000000")[:6]
                            exp = datetime.fromisoformat(f"{base}.{frac}{tz}")
                        else:
                            exp = datetime.fromisoformat(raw_exp)

                    if datetime.now(timezone.utc) > exp:
                        sb.table("app_cache").delete().eq("key", key).execute()
                        return None
                return row["value"]
            
            return await asyncio.to_thread(_get)
        except Exception as e:
            if attempt == 0 and "ConnectionTerminated" in str(type(e).__name__) or "ConnectionTerminated" in str(e):
                from db.supabase_client import refresh_supabase_client
                refresh_supabase_client()
                continue
            print(f"⚠️ Supabase Cache GET failed for {key}: {e}")
            return None

async def set_cache(key: str, value: str, expire: int = 3600):
    for attempt in range(2):
        try:
            def _set():
                sb = _get_sb()
                expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expire)).isoformat()
                sb.table("app_cache").upsert({
                    "key": key,
                    "value": str(value),
                    "expires_at": expires_at
                }).execute()
                
            await asyncio.to_thread(_set)
            return
        except Exception as e:
            if attempt == 0 and "ConnectionTerminated" in str(e):
                from db.supabase_client import refresh_supabase_client
                refresh_supabase_client()
                continue
            print(f"⚠️ Supabase Cache SET failed for {key}: {e}")

async def delete_cache(key: str):
    for attempt in range(2):
        try:
            def _del():
                sb = _get_sb()
                sb.table("app_cache").delete().eq("key", key).execute()
                
            await asyncio.to_thread(_del)
            return
        except Exception as e:
            if attempt == 0 and "ConnectionTerminated" in str(e):
                from db.supabase_client import refresh_supabase_client
                refresh_supabase_client()
                continue
            print(f"⚠️ Supabase Cache DELETE failed for {key}: {e}")

# ── High-level caching helpers (Token-Saving Strategy) ───────────────────────

async def cache_embedding(text: str, embedding: list, expire: int = 86400) -> None:
    """Cache an embedding vector for 24h. Key = first 200 chars of text."""
    key = f"emb:{text[:200]}"
    await set_cache(key, json.dumps(embedding), expire=expire)

async def get_cached_embedding(text: str) -> Optional[list]:
    """Retrieve a cached embedding. Returns None if not found."""
    key = f"emb:{text[:200]}"
    val = await get_cache(key)
    return json.loads(val) if val else None

async def cache_chunks(query: str, chunks: list, expire: int = 3600) -> None:
    """Cache ranked chunks for a query for 1h. Strips embeddings to save size."""
    key = f"chunks:{query.lower().strip()[:150]}"
    light_chunks = []
    for c in chunks:
        c_copy = c.copy()
        if "embedding" in c_copy:
            del c_copy["embedding"]
        light_chunks.append(c_copy)
    await set_cache(key, json.dumps(light_chunks), expire=expire)

async def get_cached_chunks(query: str) -> Optional[list]:
    """Retrieve cached ranked chunks. Returns None if not found."""
    key = f"chunks:{query.lower().strip()[:150]}"
    val = await get_cache(key)
    return json.loads(val) if val else None

async def cache_summary(url: str, summary: str, expire: int = 86400) -> None:
    """Cache a page summary for 24h."""
    key = f"summary:{url[:200]}"
    await set_cache(key, summary, expire=expire)

async def get_cached_summary(url: str) -> Optional[str]:
    """Retrieve a cached page summary."""
    key = f"summary:{url[:200]}"
    return await get_cache(key)

# ── Redis interface shim (used by payment_service) ───────────────────────────

class _RedisInterface:
    """Unified interface for all Redis operations."""
    def __init__(self):
        self.redis_client = self  # backward compat

    async def get(self, key): return await get_cache(key)
    async def set(self, key, val, ex=None): await set_cache(key, val, expire=ex or 3600)
    async def delete(self, key): await delete_cache(key)
    async def incr(self, key):
        val = int(await get_cache(key) or 0) + 1
        await set_cache(key, str(val)); return val
    async def decr(self, key):
        val = int(await get_cache(key) or 0) - 1
        await set_cache(key, str(val)); return val

redis_client = _RedisInterface()
