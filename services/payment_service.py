import os
import uuid
import asyncio
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from db.redis_client import redis_client
from db.supabase_client import get_supabase_client, with_supabase_retry
from .receipt_service import generate_payment_receipt
from .otp_service import send_email_with_attachment, send_custom_email

PLAN_CONFIG = {
    "free":       {"credits": 3,   "amount": 0.0},
    "student":    {"credits": 50,  "amount": 49.0},
    "researcher": {"credits": 120, "amount": 99.0},
}

# ── Persistent Credits Logic (Supabase) ───────────────────────────────────────

async def get_user_profile(user_id: str) -> dict:
    """ Reads plan and credits from Supabase with Redis cache fallback """
    cache_key = f"profile:{user_id}"
    cached = await redis_client.get(cache_key)
    if cached:
        import json
        return json.loads(cached)

    @with_supabase_retry
    def _fetch():
        sb = get_supabase_client()
        row = sb.table("user_credits").select("*").eq("user_id", user_id).execute().data
        return row[0] if row else None
        
    profile = await asyncio.to_thread(_fetch)
    if profile:
        import json
        await redis_client.set(cache_key, json.dumps(profile), ex=300) # 5m cache
    return profile

async def check_credits(user_id: str) -> bool:
    """ Enforce System Design: Check User Plan -> Limit/Deduct """
    prof = await get_user_profile(user_id)
    if not prof:
        return False
    # If student/researcher, they need > 0 credits.
    # If free, they also need > 0 credits (we give 3 initially).
    return prof.get("credits_remaining", 0) > 0

async def deduct_credits(user_id: str):
    """ Deduct 1 credit and update both Supabase and Redis Cache """
    @with_supabase_retry
    def _update():
        sb = get_supabase_client()
        # Atomic decrement via RPC or simple update
        # We'll use simple update for this demo
        curr = sb.table("user_credits").select("credits_remaining, total_spent").eq("user_id", user_id).execute().data[0]
        sb.table("user_credits").update({
            "credits_remaining": max(0, curr["credits_remaining"] - 1),
            "total_spent": curr["total_spent"] + 1,
            "updated_at": "now()"
        }).eq("user_id", user_id).execute()
        
    await asyncio.to_thread(_update)
    # Invalidate cache
    await redis_client.redis_client.delete(f"profile:{user_id}")

# ── Payment / Upgrade logic ───────────────────────────────────────────────────

async def create_payment_order(user_id: str, email: str, plan: str,
                               override_amount: float = None,
                               override_credits: int = None) -> dict:
    """ Step 1 & 2: Click Upgrade -> Create Order. Supports discounts & custom plans. """
    if plan in PLAN_CONFIG:
        cfg = dict(PLAN_CONFIG[plan])
        if override_amount is not None:
            cfg["amount"] = round(override_amount, 2)  # Apply discount
    elif override_amount is not None and override_credits is not None:
        cfg = {"amount": round(override_amount, 2), "credits": override_credits}
    else:
        return {"success": False, "error": "Invalid plan."}
    
    order_id = f"RA-{uuid.uuid4().hex[:6].upper()}"
    
    @with_supabase_retry
    def _save():
        sb = get_supabase_client()
        sb.table("pending_payments").insert({
            "user_id": user_id, "email": email, "plan": plan, 
            "amount": cfg["amount"], "order_id": order_id, "status": "pending",
            "utr_number": f"GEN-{order_id}" # Placeholder for NOT NULL constraint
        }).execute()
    
    await asyncio.to_thread(_save)
    
    # Generate UPI QR
    import urllib.parse
    upi_url = f"upi://pay?pa={os.getenv('UPI_ID', '9693932656@ptyes')}&pn=ResearchAgent&am={cfg['amount']}&tn=Order_{order_id}"
    qr = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={urllib.parse.quote(upi_url)}"
    
    return {"success": True, "order_id": order_id, "qr_image_url": qr, "amount": cfg["amount"], "credits": cfg.get("credits")}

async def verify_payment_by_order(user_id: str, order_id: str, utr_number: str) -> dict:
    """ 
    User pays -> Enters UTR -> Set status to 'verifying' 
    NO credits are awarded yet. Admin must manually approve.
    """
    @with_supabase_retry
    def _verify():
        sb = get_supabase_client()
        res = sb.table("pending_payments").select("*").eq("order_id", order_id).execute().data
        if not res or res[0]["user_id"] != user_id or res[0]["status"] != "pending":
            return None
        return res[0]

    order = await asyncio.to_thread(_verify)
    if not order: return {"success": False, "error": "Invalid or already processed order."}

    @with_supabase_retry
    def _update_to_verifying():
        sb = get_supabase_client()
        sb.table("pending_payments").update({
            "status": "verifying", 
            "utr_number": utr_number,
            "submitted_at": "now()"
        }).eq("order_id", order_id).execute()

    await asyncio.to_thread(_update_to_verifying)
    return {"success": True, "message": "Payment reference submitted for manual verification. Credits will be added once approved (usually 12-24h)."}

async def approve_payment_admin(order_id: str, admin_email: str) -> dict:
    """ Admin tool: Verify UTR against bank records and award credits """
    @with_supabase_retry
    def _get_order():
        sb = get_supabase_client()
        res = sb.table("pending_payments").select("*").eq("order_id", order_id).execute().data
        return res[0] if res else None

    order = await asyncio.to_thread(_get_order)
    if not order: return {"success": False, "error": "Order not found."}
    if order["status"] != "verifying": return {"success": False, "error": f"Order is in {order['status']} status, not 'verifying'."}

    user_id = order["user_id"]
    plan = order["plan"]
    
    # Resolve credits: built-in plan OR custom plan fetched from platform_config
    if plan in PLAN_CONFIG:
        new_credits = PLAN_CONFIG[plan]["credits"]
    else:
        try:
            import json
            sb_tmp = get_supabase_client()
            cfg_res = await asyncio.to_thread(lambda: sb_tmp.table("platform_config").select("config_value").eq("config_key", "custom_plans").execute())
            custom_plans_raw = json.loads(cfg_res.data[0]["config_value"] or "[]") if cfg_res.data else []
            plan_def = next((p for p in custom_plans_raw if p.get("id") == plan), None)
            new_credits = int(plan_def["credits"]) if plan_def else 50
        except Exception as e:
            print(f"Custom plan credits lookup failed: {e}")
            new_credits = 50  # safe fallback

    @with_supabase_retry
    def _activate():
        sb = get_supabase_client()
        # 1. Update Payment status
        sb.table("pending_payments").update({
            "status": "approved", 
            "verified_at": datetime.utcnow().isoformat(),
            "verified_by": admin_email
        }).eq("order_id", order_id).execute()
        
        # 2. Add Credits & Set Plan
        res = sb.table("user_credits").select("credits_remaining").eq("user_id", user_id).execute().data
        if not res:
            sb.table("user_credits").insert({"user_id": user_id, "plan": plan, "credits_remaining": new_credits}).execute()
        else:
            curr_credits = res[0]["credits_remaining"]
            sb.table("user_credits").update({
                "plan": plan,
                "credits_remaining": curr_credits + new_credits,
                "updated_at": datetime.utcnow().isoformat()
            }).eq("user_id", user_id).execute()

    # We now call with thread directly as _activate is decorated
    await asyncio.to_thread(_activate)

    # Use the safe delete wrapper
    await redis_client.delete(f"profile:{user_id}") 
    
    # 3. Generate & Send Receipt PDF
    try:
        pdf_bytes = generate_payment_receipt({
            "email": order["email"],
            "plan": plan,
            "amount": order["amount"],
            "transaction_id": order.get("utr_number", order_id),
            "credits": new_credits
        })
        subject = f"Your {plan.upper()} Activation Receipt - Surefact"
        html = f"""
        <h2>Payment Approved!</h2>
        <p>Hello, your payment (Order: {order_id}) has been manually verified. 
        Your <b>{plan.upper()}</b> plan is now active with <b>{new_credits}</b> credits.</p>
        <p>Please find your official receipt attached.</p>
        """
        asyncio.create_task(send_email_with_attachment(order["email"], subject, html, pdf_bytes, f"Receipt_{order_id}.pdf"))
    except Exception as e:
        print(f"⚠️ Failed to send payment receipt: {e}")

    return {"success": True, "plan": plan, "credits": new_credits, "message": f"Activated {plan} plan for user."}

async def reject_payment_admin(order_id: str, reason: str) -> dict:
    """ Admin tool: Reject fraudulent or invalid payment references """
    @with_supabase_retry
    def _reject():
        sb = get_supabase_client()
        # 1. Fetch email first
        res = sb.table("pending_payments").select("email").eq("order_id", order_id).execute().data
        email = res[0]["email"] if res else None

        # 2. Update status
        sb.table("pending_payments").update({
            "status": "rejected", 
            "rejection_reason": reason,
            "verified_at": "now()"
        }).eq("order_id", order_id).execute()
        return email
        
    email = await asyncio.to_thread(_reject)
    
    # Send Rejection Email
    if email:
        try:
            subject = "Action Required: Payment Reference Rejected"
            html = f"""
            <h2 style='color:red;'>Payment Verification Failed</h2>
            <p>Your payment reference (Order: {order_id}) was rejected for the following reason:</p>
            <blockquote style='background:#f9f9f9; padding:10px; border-left:4px solid red;'>{reason}</blockquote>
            <p>If you believe this is an error, please contact support or try upgrading again with a valid UTR.</p>
            """
            asyncio.create_task(send_custom_email(email, subject, html))
        except: pass

    return {"success": True, "message": "Payment rejected."}

async def initialize_user_if_needed(user_id: str):
    """ Ensure user has a profile in Supabase (backup for the trigger) """
    @with_supabase_retry
    def _init():
        sb = get_supabase_client()
        exists = sb.table("user_credits").select("user_id").eq("user_id", user_id).execute().data
        if not exists:
            sb.table("user_credits").insert({"user_id": user_id, "plan": "free", "credits_remaining": 3}).execute()
    await asyncio.to_thread(_init)

# ── Refund Logic (System Expansion) ──────────────────────────────────────────

async def calculate_refundable_amount(user_id: str) -> dict:
    """ 
    Calculate refund as: Max(0, PaidAmt * (CreditsRem / CreditsTotal) * (TimeRem / 30))
    """
    @with_supabase_retry
    def _fetch_last_payment():
        sb = get_supabase_client()
        # Get the most recent approved payment
        res = sb.table("pending_payments").select("*").eq("user_id", user_id).eq("status", "approved").order("verified_at", desc=True).limit(1).execute().data
        if not res: return None
        return res[0]

    @with_supabase_retry
    def _get_credit_profile():
        sb = get_supabase_client()
        res = sb.table("user_credits").select("*").eq("user_id", user_id).execute().data
        return res[0] if res else None

    payment = await asyncio.to_thread(_fetch_last_payment)
    profile = await asyncio.to_thread(_get_credit_profile)

    if not payment or not profile or profile["plan"] == "free":
        return {"success": False, "error": "No premium plan found to refund."}

    # Credits factor
    total_paid = float(payment.get("amount", 0))
    plan_name = payment.get("plan", "free")
    credits_total = PLAN_CONFIG.get(plan_name, {}).get("credits", 1)
    credits_rem = profile.get("credits_remaining", 0)
    
    # Time factor (30 days window)
    try:
        v_at = str(payment.get("verified_at", ""))
        if ' ' in v_at and 'T' not in v_at: v_at = v_at.replace(' ', 'T')
        if not v_at.endswith('Z') and '+' not in v_at: v_at += 'Z'
        verified_at = datetime.fromisoformat(v_at.replace("Z", "+00:00"))
    except:
        verified_at = datetime.now(timezone.utc) - timedelta(days=1) # Fallback to 1 day ago
        
    now = datetime.now(timezone.utc)
    days_used = (now - verified_at).days
    time_rem_factor = max(0, (30 - days_used) / 30)
    credit_rem_factor = max(0, credits_rem / credits_total)
    
    # Final Refund (Credits * Time * Total)
    refund_amt = round(total_paid * credit_rem_factor * time_rem_factor, 2)
    
    return {
        "success": True, 
        "amount_paid": total_paid,
        "credits_remaining": credits_rem,
        "days_remaining": max(0, 30 - days_used),
        "estimated_refund": refund_amt,
        "plan": plan_name,
        "order_id": payment.get("order_id", "manual")
    }

async def submit_refund_request(user_id: str, email: str, bank_details: dict, survey_results: Optional[dict] = None) -> dict:
    """ Processes user cancellation, downgrades plan, and logs refund request """
    res = await calculate_refundable_amount(user_id)
    if not res.get("success"): return res
    
    if res["estimated_refund"] <= 0:
        return {"success": False, "error": "Refund amount is 0. Subscription usage exceeded."}

    now_iso = datetime.now(timezone.utc).isoformat()

    @with_supabase_retry
    def _submit():
        sb = get_supabase_client()
        # 1. Create Refund Request via Secure RPC (bypasses RLS)
        sb.rpc("create_refund_request", {
            "p_user_id": user_id, 
            "p_email": email, 
            "p_plan": res["plan"],
            "p_amount_paid": res["amount_paid"],
            "p_credits_at_cancellation": res["credits_remaining"],
            "p_refund_amount": res["estimated_refund"],
            "p_bank_details": bank_details,
            "p_survey_results": survey_results
        }).execute()
        
        # 2. Revert User to Free Plan & Wipe Premium Credits
        sb.table("user_credits").update({
            "plan": "free",
            "credits_remaining": 3,
            "updated_at": now_iso
        }).eq("user_id", user_id).execute()

    await asyncio.to_thread(_submit)
    
    # Invalidate Cache
    await redis_client.delete(f"profile:{user_id}")
    
    return {"success": True, "message": "Refund request submitted. Access reverted to Free Plan."}

async def get_user_payments(user_id: str) -> list:
    """ Fetches all transaction records for the given user """
    @with_supabase_retry
    def _fetch():
        sb = get_supabase_client()
        return (sb.table("pending_payments")
                  .select("*")
                  .eq("user_id", user_id)
                  .order("submitted_at", desc=True)
                  .execute().data or [])
    
    return await asyncio.to_thread(_fetch)

async def get_pending_verifications_admin() -> list:
    """ Fetches all payments waiting for admin verification """
    @with_supabase_retry
    def _fetch():
        sb = get_supabase_client()
        return (sb.table("pending_payments")
                  .select("*")
                  .eq("status", "verifying")
                  .order("submitted_at", desc=True)
                  .execute().data or [])
    
    return await asyncio.to_thread(_fetch)
