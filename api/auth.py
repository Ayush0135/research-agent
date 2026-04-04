from typing import Optional, List
from fastapi import APIRouter, HTTPException, status, Depends, Request
from pydantic import BaseModel, EmailStr
from db.supabase_client import get_supabase_client
from services.otp_service import send_otp, verify_otp
from db.redis_client import redis_client
from services.suspension_service import check_suspension
import asyncio

router = APIRouter(prefix="/auth", tags=["Authentication"])

# ── Request Schemas ──────────────────────────────────────────────────────────

class SignUpRequest(BaseModel):
    email: EmailStr
    password: str

class SignInRequest(BaseModel):
    email: EmailStr
    password: str

class OTPVerifyRequest(BaseModel):
    email: EmailStr
    token: str   # The 6-digit OTP

class OTPRequest(BaseModel):
    email: EmailStr

class AppealRequest(BaseModel):
    email: EmailStr
    reason: str

class AuthResponse(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    user_id: str
    email: str
    username: Optional[str] = None
    has_profile: bool = False

class ProfileUpdateRequest(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    school_college: Optional[str] = None
    enrollment_number: Optional[str] = None
    username: str

class AdminOTPRequest(BaseModel):
    email: str

class AdminOTPVerifyRequest(BaseModel):
    email: str
    token: str

# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(payload: SignUpRequest):
    """
    Step 1: Check if user already exists in Supabase.
    Step 2: Generate OTP and temporarily store (email, password) in Redis.
    Step 3: Send OTP via email. User is NOT saved to the database yet.
    """
    try:
        # ── Ban Enforcement ──
        ban = await check_suspension(payload.email)
        if ban:
            raise HTTPException(
                status_code=403,
                detail=f"Your account is suspended until {ban['expires_at'][:10]}. Reason: {ban['reason_code'].replace('_', ' ').title()}. Check your email for details."
            )

        sb = get_supabase_client()
        import os, httpx
        SUPABASE_URL = os.getenv("SUPABASE_URL", "")
        SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_KEY", ""))

        # Check if user already exists
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{SUPABASE_URL}/auth/v1/admin/users",
                headers={"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"},
                params={"email": payload.email},
                timeout=10.0
            )
            users = resp.json().get("users", [])
            if users:
                raise HTTPException(status_code=400, detail="User already registered.")

        # Defer registration -> store password in Redis & send OTP
        await send_otp(payload.email, password=payload.password)

        return {
            "message": "A 6-digit OTP has been sent to your email.",
            "requires_otp": True,
            "email": payload.email
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/login", response_model=AuthResponse)
async def login(payload: SignInRequest):
    """Sign in with email + password. Returns a fresh JWT."""
    try:
        # ── Ban Enforcement ──
        ban = await check_suspension(payload.email)
        if ban:
            raise HTTPException(
                status_code=403,
                detail=f"Your account is suspended until {ban['expires_at'][:10]}. Reason: {ban['reason_code'].replace('_', ' ').title()}. Check your email for details."
            )

        sb = get_supabase_client()

        def _login():
            return sb.auth.sign_in_with_password({
                "email": payload.email,
                "password": payload.password
            })
        response = await asyncio.to_thread(_login)

        if not response.user or not response.session:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")

        # Check for profile
        def _get_profile():
            res = sb.table("users").select("username").eq("id", str(response.user.id)).execute().data
            return res[0] if res else None
        profile = await asyncio.to_thread(_get_profile)

        return AuthResponse(
            access_token=response.session.access_token,
            refresh_token=response.session.refresh_token,
            user_id=str(response.user.id),
            email=response.user.email,
            username=profile.get("username") if profile else None,
            has_profile=bool(profile and profile.get("username"))
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))


@router.post("/send-otp")
async def send_otp_endpoint(payload: OTPRequest):
    """Resend a fresh 6-digit OTP to the given email."""
    try:
        await send_otp(payload.email)
        return {"message": f"A fresh OTP has been sent to {payload.email}."}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/verify-otp")
async def verify_otp_endpoint(payload: OTPVerifyRequest):
    """
    Verify the 6-digit OTP (stored in Redis). 
    If this is a signup (password found in cache), we CREATE the user and allocate credits now.
    """
    try:
        # 1. Check OTP against Redis (returns dict on success)
        otp_data = await verify_otp(payload.email, payload.token)
        if not otp_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired OTP. Please request a new one."
            )

        password = otp_data.get("password")
        sb = get_supabase_client()

        if password:
            # ── DEFERRED SIGNUP FLOW ──
            # They proved they own the email, NOW we create the account and allocate credits.
            def _signup():
                return sb.auth.sign_up({
                    "email": payload.email,
                    "password": password
                })
            response = await asyncio.to_thread(_signup)

            if not response.user:
                raise HTTPException(status_code=400, detail="Signup failed during verification.")
            
            user_id = str(response.user.id)
            
            # ALLOCATE SERVICES: give them 3 free credits only upon successful OTP
            from services.payment_service import initialize_user_if_needed
            await initialize_user_if_needed(user_id)

            # Register in suspensions monitoring table (clean record for admin visibility)
            from services.suspension_service import register_user_for_monitoring
            await register_user_for_monitoring(user_id, payload.email)

            if response.session and response.session.access_token:
                return {
                    "access_token": response.session.access_token,
                    "refresh_token": response.session.refresh_token,
                    "token_type": "bearer",
                    "user_id": user_id,
                    "email": payload.email,
                    "username": None,
                    "has_profile": False,
                    "message": "✅ Verification complete! Account fully created."
                }
            
            return {"verified": True, "email": payload.email, "message": "✅ Account created successfully! Please log in."}
        
        else:
            # ── PASSWORDLESS LOGIN FLOW ──
            # Just OTP verification without password (legacy or re-verify)
            import os, httpx
            SUPABASE_URL = os.getenv("SUPABASE_URL", "")
            SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_KEY", ""))

            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{SUPABASE_URL}/auth/v1/admin/users",
                    headers={"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"},
                    params={"email": payload.email},
                    timeout=10.0
                )
                users = resp.json().get("users", [])
                if not users:
                    raise HTTPException(status_code=400, detail="User not found.")

                return {
                    "verified": True,
                    "email": payload.email,
                    "message": "✅ Email verified! Please sign in with your password to continue."
                }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))



@router.post("/logout")
async def logout(token: str):
    try:
        sb = get_supabase_client()
        await asyncio.to_thread(sb.auth.sign_out)
        return {"message": "Logged out successfully."}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/me")
async def get_me(token: str):
    try:
        sb = get_supabase_client()
        def _get_user():
            return sb.auth.get_user(token)
        response = await asyncio.to_thread(_get_user)

        if not response or not response.user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")

        user_id = str(response.user.id)
        from services.payment_service import get_user_profile
        profile = await get_user_profile(user_id)

        return {
            "user_id": user_id,
            "email": response.user.email,
            "plan": profile.get("plan", "free") if profile else "free",
            "credits_remaining": profile.get("credits_remaining", 0) if profile else 0,
            "full_name": profile.get("full_name"),
            "username": profile.get("username"),
            "phone": profile.get("phone"),
            "school_college": profile.get("school_college"),
            "enrollment_number": profile.get("enrollment_number")
        }
    except HTTPException:
        raise
    except Exception as e:
        err_msg = str(e).lower()
        if any(w in err_msg for w in ["invalid", "expired", "not found"]):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired or invalid.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@router.post("/refresh", response_model=AuthResponse)
async def refresh_token(refresh_token: str):
    """Refreshes the access token using a refresh token."""
    try:
        sb = get_supabase_client()
        def _refresh():
            return sb.auth.refresh_session(refresh_token)
        response = await asyncio.to_thread(_refresh)

        if not response or not response.session:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token.")

        # Check for profile
        def _get_profile():
            res = sb.table("users").select("username").eq("id", str(response.user.id)).execute().data
            return res[0] if res else None
        profile = await asyncio.to_thread(_get_profile)

        return AuthResponse(
            access_token=response.session.access_token,
            refresh_token=response.session.refresh_token,
            user_id=str(response.user.id),
            email=response.user.email,
            username=profile.get("username") if profile else None,
            has_profile=bool(profile and profile.get("username"))
        )
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Refresh failed: {str(e)}")

@router.post("/update-profile")
async def update_profile(payload: ProfileUpdateRequest, token: str):
    """Update user profile details. Mandatory: username."""
    try:
        print(f"DEBUG: Processing profile update for username: {payload.username}")
        sb = get_supabase_client()
        
        # Verify token and get user
        def _get_user():
            return sb.auth.get_user(token)
        response = await asyncio.to_thread(_get_user)
        
        if not response or not response.user:
            raise HTTPException(status_code=401, detail="Invalid session or token.")
        
        user_id = str(response.user.id)
        
        # 1. Check if username is taken by anyone else
        def _check_username():
            res = sb.table("users").select("id").eq("username", payload.username).neq("id", user_id).execute().data
            return len(res) > 0
        
        if await asyncio.to_thread(_check_username):
            raise HTTPException(status_code=400, detail="Username already taken.")

        # 2. Update profiles in a robust sync
        def _sync_profile():
            print(f"DEBUG: Updating Supabase tables for user {user_id}")
            # Update public.users
            sb.table("users").upsert({
                "id": user_id,
                "email": response.user.email,
                "username": payload.username,
                "full_name": payload.full_name,
                "phone": payload.phone,
                "school_college": payload.school_college,
                "enrollment_number": payload.enrollment_number
            }, on_conflict="id").execute()
            
            # Update user_credits (sync username for UI)
            sb.table("user_credits").update({"username": payload.username}).eq("user_id", user_id).execute()
        
        await asyncio.to_thread(_sync_profile)
        
        # 3. Clear Cache
        await redis_client.delete(f"profile:{user_id}")
        print(f"SUCCESS: Profile updated for {payload.username}")
        
        return {"success": True, "message": "Profile updated successfully."}
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e))

# ── ADMIN AUTHENTICATION ─────────────────────────────────────────────────────

@router.post("/admin/send-otp")
async def admin_send_otp(payload: AdminOTPRequest):
    """Send OTP specifically for users in the admin_users employee registry."""
    email = payload.email.lower().strip()
    sb = get_supabase_client()
    res = await asyncio.to_thread(lambda: sb.table("admin_users").select("email").eq("email", email).execute())
    
    if not res.data:
        raise HTTPException(status_code=403, detail=f"Unauthorized: {email} not found in employee registry.")
        
    await send_otp(email)
    return {"message": "Admin OTP Sent"}

@router.post("/admin/verify-otp")
async def admin_verify_otp(payload: AdminOTPVerifyRequest):
    """Verify admin identity and issue a secure Terminal Session Token (Redis-backed)."""
    email = payload.email.lower().strip()
    
    # 1. Verify OTP with Redis
    otp_data = await verify_otp(email, payload.token)
    if not otp_data:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP.")
    
    # 2. Final security check
    sb = get_supabase_client()
    res = await asyncio.to_thread(lambda: sb.table("admin_users").select("email").eq("email", email).execute())
    if not res.data:
        raise HTTPException(status_code=403, detail="Employee clearance revoked during login.")
    
    # 3. Issue session token
    import uuid
    admin_token = uuid.uuid4().hex
    await redis_client.set(f"admin_session:{admin_token}", email, ex=86400) # 24h
    
    return {
        "access_token": admin_token,
        "email": email,
        "message": "Admin Terminal Access Granted."
    }
@router.get("/config")
async def get_public_config():
    """Fetches public bits of platform configuration (e.g. broadcast message)."""
    try:
        sb = get_supabase_client()
        # Fetch only keys allowed for public users
        res = await asyncio.to_thread(lambda: sb.table("platform_config").select("config_key, config_value").in_("config_key", ["broadcast_message", "maintenance_mode", "ticker_text", "ticker_enabled", "ticker_style", "ticker_speed", "discount_percent", "discount_enabled", "discount_label", "custom_plans"]).execute())
        data = {item['config_key']: item['config_value'] for item in res.data}
        
        # Normalize booleans for frontend consistency
        for key in ["ticker_enabled", "discount_enabled"]:
            if key in data:
                val = data[key]
                data[key] = val in [True, "true", "True", 1, "1"]
        
        return data
    except Exception as e:
        print(f"Public config error: {e}")
        return {}

@router.get("/profile")
async def get_profile_v2(request: Request):
    """Fetches full user profile using bearer token from header."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    token = auth_header.split(" ")[1]
    return await get_me(token)

@router.post("/submit-appeal")
async def submit_suspension_appeal(payload: AppealRequest):
    """Public endpoint for suspended users to appeal their ban."""
    from services.suspension_service import submit_appeal
    result = await submit_appeal(payload.email, payload.reason)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result
