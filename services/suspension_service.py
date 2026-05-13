"""
Account Suspension Service
Handles admin-initiated user bans with PDF notices and email delivery.
"""
import asyncio
from typing import Optional, Dict
from datetime import datetime, timezone, timedelta
from db.supabase_client import get_supabase_client
from db.redis_client import redis_client
from services.receipt_service import generate_suspension_notice
from services.otp_service import send_email_with_attachment

# ── Suspension Reason Registry ────────────────────────────────────────────────
# Each reason has a user-facing label and a detailed legal explanation.

SUSPENSION_REASONS = {
    "abusive_language": {
        "label": "🗣️ Abusive Language",
        "detail": (
            "Your account has been suspended due to the use of abusive, threatening, "
            "or harassing language directed at other users or platform staff. "
            "This violates Section 3.2 of our Terms of Service regarding respectful communication. "
            "Continued violations may result in permanent account termination."
        )
    },
    "misconduct": {
        "label": "⚠️ Misconduct",
        "detail": (
            "Your account has been suspended for repeated violations of platform conduct guidelines, "
            "including but not limited to: misuse of research tools, spamming queries, "
            "or attempting to manipulate system resources. "
            "Our platform is designed for legitimate academic and professional research only."
        )
    },
    "inappropriate_content": {
        "label": "🔞 Inappropriate / Sexual Content",
        "detail": (
            "Your account has been suspended for generating, requesting, or distributing "
            "sexually explicit, obscene, or otherwise inappropriate content through the research platform. "
            "This is a zero-tolerance policy under Section 4.1 of our Terms of Service. "
            "Any further attempts will result in a permanent ban."
        )
    },
    "fraud_attempt": {
        "label": "💳 Fraud / Payment Abuse",
        "detail": (
            "Your account has been suspended due to suspected fraudulent activity, "
            "including submission of fake payment references (UTR), chargeback abuse, "
            "or attempts to exploit the credit system. "
            "We take financial integrity seriously and may report fraudulent activity to authorities."
        )
    },
    "tos_violation": {
        "label": "📜 Terms of Service Violation",
        "detail": (
            "Your account has been suspended for a general violation of our Terms of Service. "
            "This may include unauthorized scraping, API abuse, account sharing, "
            "or other activities that compromise platform integrity and security. "
            "Please review our Terms of Service for a complete list of prohibited activities."
        )
    }
}

BAN_DURATION_DAYS = 3


async def suspend_user(user_id: str, email: str, reason_code: str, admin_email: str, notes: str = None) -> dict:
    """
    Full suspension pipeline:
    1. Validate reason code
    2. Insert suspension record into DB
    3. Revoke user credits and mark plan as 'suspended'
    4. Generate PDF Suspension Notice
    5. Send branded email with PDF attachment
    6. Invalidate all cached sessions
    """
    if reason_code not in SUSPENSION_REASONS:
        return {"success": False, "error": f"Invalid reason code: {reason_code}"}

    reason = SUSPENSION_REASONS[reason_code]
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=BAN_DURATION_DAYS)

    # 1. Snapshot current plan/credits before suspending
    def _snapshot_and_suspend():
        sb = get_supabase_client()
        # Get current plan/credits
        profile = sb.table("user_credits").select("plan, credits_remaining").eq("user_id", user_id).execute().data
        original_plan = profile[0]["plan"] if profile else "free"
        original_credits = profile[0]["credits_remaining"] if profile else 0

        # Update existing clean record OR insert new one (upsert by email)
        # First try to update existing record for this email
        existing = sb.table("account_suspensions").select("id").eq("email", email.lower().strip()).eq("status", "clean").execute().data
        
        suspension_data = {
            "user_id": user_id,
            "email": email.lower().strip(),
            "reason_code": reason_code,
            "reason_detail": reason["detail"],
            "suspended_by": admin_email,
            "suspended_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "status": "active",
            "notes": notes,
            "original_plan": original_plan,
            "original_credits": original_credits
        }

        if existing:
            # Update the existing clean record to active
            sb.table("account_suspensions").update(suspension_data).eq("id", existing[0]["id"]).execute()
        else:
            # No clean record exists, insert fresh
            sb.table("account_suspensions").insert(suspension_data).execute()

        # Revoke credits and mark plan as suspended
        sb.table("user_credits").update({
            "plan": "suspended",
            "credits_remaining": 0,
            "updated_at": now.isoformat()
        }).eq("user_id", user_id).execute()

    await asyncio.to_thread(_snapshot_and_suspend)

    # 3. Invalidate cached profile
    await redis_client.delete(f"profile:{user_id}")

    # 4. Generate PDF Notice
    try:
        pdf_bytes = generate_suspension_notice({
            "email": email,
            "reason_label": reason["label"],
            "reason_detail": reason["detail"],
            "suspended_at": now.strftime("%Y-%m-%d %H:%M UTC"),
            "expires_at": expires_at.strftime("%Y-%m-%d %H:%M UTC"),
            "duration_days": BAN_DURATION_DAYS
        })
    except Exception as e:
        print(f"⚠️ PDF generation failed: {e}")
        pdf_bytes = None

    # 5. Send branded email with PDF
    html_content = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;padding:40px 20px;background:#0a0a0f;color:#e2e8f0;border-radius:16px;border:1px solid #1e1e2e">
      <div style="text-align:center;margin-bottom:24px">
        <span style="font-size:1.2rem;font-weight:700;color:#ef4444">⛔ Account Suspended</span>
      </div>
      
      <p style="color:#94a3b8;font-size:0.9rem;line-height:1.7">
        Dear User,<br><br>
        We regret to inform you that your account (<strong style="color:#fff">{email}</strong>) 
        has been <strong style="color:#ef4444">suspended</strong> by our administration team.
      </p>
      
      <div style="background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);border-radius:12px;padding:20px;margin:20px 0">
        <p style="color:#ef4444;font-weight:700;margin:0 0 8px;font-size:0.85rem">REASON: {reason['label']}</p>
        <p style="color:#94a3b8;font-size:0.85rem;margin:0;line-height:1.6">{reason['detail']}</p>
      </div>
      
      <div style="background:rgba(255,255,255,0.03);border-radius:12px;padding:16px;margin:20px 0">
        <p style="margin:0;font-size:0.85rem;color:#94a3b8">
          <strong style="color:#fff">Suspension Period:</strong> {BAN_DURATION_DAYS} days<br>
          <strong style="color:#fff">From:</strong> {now.strftime('%B %d, %Y at %H:%M UTC')}<br>
          <strong style="color:#fff">Until:</strong> {expires_at.strftime('%B %d, %Y at %H:%M UTC')}
        </p>
      </div>
      
      <p style="color:#64748b;font-size:0.8rem;line-height:1.6;margin-top:24px">
        During this period, you will not be able to log in or create a new account. 
        If you believe this action was taken in error, please reply to this email 
        or contact our support team after the suspension period ends.
      </p>
      
      <p style="color:#64748b;font-size:0.75rem;margin-top:32px;padding-top:16px;border-top:1px solid rgba(255,255,255,0.05)">
        Please find the official Suspension Notice attached to this email for your records.<br>
        — The Surefact Administration Team
      </p>
    </div>
    """

    subject = f"⛔ Account Suspended — {reason['label']}"

    if pdf_bytes:
        asyncio.create_task(send_email_with_attachment(
            email, subject, html_content, pdf_bytes,
            f"Suspension_Notice_{now.strftime('%Y%m%d')}.pdf"
        ))
    else:
        from services.otp_service import send_custom_email
        asyncio.create_task(send_custom_email(email, subject, html_content))

    return {
        "success": True,
        "message": f"User {email} suspended for {BAN_DURATION_DAYS} days.",
        "reason": reason_code,
        "expires_at": expires_at.isoformat()
    }


async def check_suspension(email: str) -> Optional[dict]:
    """
    Check if an email is currently under an active suspension.
    Returns the suspension record if banned, None if clear.
    Auto-expires old bans.
    """
    def _check():
        sb = get_supabase_client()
        res = (sb.table("account_suspensions")
               .select("*")
               .eq("email", email.lower().strip())
               .eq("status", "active")
               .order("expires_at", desc=True)
               .limit(1)
               .execute().data)
        return res[0] if res else None

    record = await asyncio.to_thread(_check)
    if not record:
        return None

    # Check if ban has expired
    expires_at_str = record["expires_at"].replace("Z", "+00:00")
    try:
        expires_at = datetime.fromisoformat(expires_at_str)
    except ValueError:
        return None

    if datetime.now(timezone.utc) > expires_at:
        # Auto-expire the ban and restore to clean status
        def _expire_and_restore():
            sb = get_supabase_client()
            # Reset record back to clean (single row per user design)
            sb.table("account_suspensions").update({
                "status": "clean",
                "reason_code": None,
                "reason_detail": None,
                "suspended_by": None,
                "expires_at": None,
                "notes": f"Auto-expired. Previous ban: {record.get('reason_code')} until {record.get('expires_at')}"
            }).eq("id", record["id"]).execute()

            # Restore original plan and credits
            original_plan = record.get("original_plan", "free")
            original_credits = record.get("original_credits", 3)
            sb.table("user_credits").update({
                "plan": original_plan,
                "credits_remaining": original_credits
            }).eq("user_id", record["user_id"]).execute()

        await asyncio.to_thread(_expire_and_restore)
        # Clear cached profile so user gets fresh data
        await redis_client.delete(f"profile:{record['user_id']}")
        return None

    return record


async def revoke_suspension(suspension_id: str, admin_email: str) -> dict:
    """Admin can manually lift a suspension early."""
    def _revoke():
        sb = get_supabase_client()
        res = sb.table("account_suspensions").select("user_id, email, original_plan, original_credits, reason_code").eq("id", suspension_id).execute().data
        if not res:
            return None
        
        # Reset record back to clean (single row per user design)
        sb.table("account_suspensions").update({
            "status": "clean",
            "reason_code": None,
            "reason_detail": None,
            "suspended_by": None,
            "expires_at": None,
            "notes": f"Revoked by {admin_email} on {datetime.now(timezone.utc).isoformat()}. Previous: {res[0].get('reason_code')}"
        }).eq("id", suspension_id).execute()

        # Restore original plan and credits
        original_plan = res[0].get("original_plan", "free")
        original_credits = res[0].get("original_credits", 3)
        sb.table("user_credits").update({
            "plan": original_plan,
            "credits_remaining": original_credits
        }).eq("user_id", res[0]["user_id"]).execute()

        return res[0]

    result = await asyncio.to_thread(_revoke)
    if not result:
        return {"success": False, "error": "Suspension record not found."}

    original_plan = result.get('original_plan', 'free')
    await redis_client.delete(f"profile:{result['user_id']}")
    return {"success": True, "message": f"Suspension revoked for {result['email']}. Restored to {original_plan.upper()} plan."}


async def get_all_suspensions() -> list:
    """Fetch all suspension/monitoring records for the admin dashboard."""
    def _fetch():
        sb = get_supabase_client()
        return (sb.table("account_suspensions")
                .select("*")
                .order("suspended_at", desc=True)
                .execute().data or [])
    return await asyncio.to_thread(_fetch)


async def register_user_for_monitoring(user_id: str, email: str):
    """Register a new user in the suspensions table with 'clean' status for admin monitoring."""
    try:
        def _register():
            sb = get_supabase_client()
            # Check if already exists (avoid duplicates)
            existing = sb.table("account_suspensions").select("id").eq("email", email.lower().strip()).execute().data
            if existing:
                return  # Already tracked
            
            sb.table("account_suspensions").insert({
                "user_id": user_id,
                "email": email.lower().strip(),
                "status": "clean",
                "suspended_at": datetime.now(timezone.utc).isoformat()
            }).execute()
        
        await asyncio.to_thread(_register)
    except Exception as e:
        print(f"⚠️ Monitoring registration failed for {email}: {e}")


# ── APPEAL SYSTEM ────────────────────────────────────────────────────────────

async def submit_appeal(email: str, appeal_reason: str) -> dict:
    """Allow a suspended user to submit an appeal."""
    # First verify they actually have an active suspension
    record = await check_suspension(email)
    if not record:
        return {"success": False, "error": "No active suspension found for this email."}

    # Check for existing pending appeal (one at a time)
    def _check_existing():
        sb = get_supabase_client()
        return sb.table("suspension_appeals").select("id").eq("email", email.lower().strip()).eq("status", "pending").execute().data

    existing = await asyncio.to_thread(_check_existing)
    if existing:
        return {"success": False, "error": "You already have a pending appeal. Please wait for admin review."}

    def _submit():
        sb = get_supabase_client()
        sb.table("suspension_appeals").insert({
            "suspension_id": record["id"],
            "email": email.lower().strip(),
            "user_id": record.get("user_id"),
            "appeal_reason": appeal_reason,
            "status": "pending"
        }).execute()

    await asyncio.to_thread(_submit)

    return {
        "success": True,
        "message": "Your appeal has been submitted. Our admin team will review it within 24 hours."
    }


async def get_all_appeals() -> list:
    """Fetch all appeals for admin dashboard."""
    def _fetch():
        sb = get_supabase_client()
        return (sb.table("suspension_appeals")
                .select("*")
                .order("created_at", desc=True)
                .execute().data or [])
    return await asyncio.to_thread(_fetch)


async def process_appeal(appeal_id: str, decision: str, admin_email: str, admin_response: str = None) -> dict:
    """
    Admin processes an appeal.
    decision: 'approved' or 'rejected'
    If approved → revoke the suspension automatically.
    """
    if decision not in ("approved", "rejected"):
        return {"success": False, "error": "Decision must be 'approved' or 'rejected'."}

    def _get_appeal():
        sb = get_supabase_client()
        return sb.table("suspension_appeals").select("*").eq("id", appeal_id).execute().data

    appeal_data = await asyncio.to_thread(_get_appeal)
    if not appeal_data:
        return {"success": False, "error": "Appeal not found."}

    appeal = appeal_data[0]
    if appeal["status"] != "pending":
        return {"success": False, "error": f"Appeal already {appeal['status']}."}

    now = datetime.now(timezone.utc)

    # Update appeal record
    def _update_appeal():
        sb = get_supabase_client()
        sb.table("suspension_appeals").update({
            "status": decision,
            "admin_response": admin_response or ("Appeal approved. Your account has been restored." if decision == "approved" else "Appeal denied. The suspension remains in effect."),
            "reviewed_by": admin_email,
            "reviewed_at": now.isoformat()
        }).eq("id", appeal_id).execute()

    await asyncio.to_thread(_update_appeal)

    # If approved, revoke the suspension
    if decision == "approved" and appeal.get("suspension_id"):
        result = await revoke_suspension(appeal["suspension_id"], admin_email)
        if not result.get("success"):
            print(f"⚠️ Appeal approved but revoke failed: {result}")

    # Send email notification to user
    try:
        from services.otp_service import send_custom_email, send_email_with_attachment
        from services.receipt_service import generate_appeal_decision_notice
        
        # Generate the PDF Decision Notice
        try:
            pdf_bytes = generate_appeal_decision_notice({
                "email": appeal["email"],
                "decision": decision,
                "admin_response": admin_response or ("Appeal approved." if decision == "approved" else "Appeal denied."),
                "decision_date": now.strftime("%Y-%m-%d %H:%M UTC")
            })
        except Exception as pdf_err:
            print(f"⚠️ PDF appeal notice generation failed: {pdf_err}")
            pdf_bytes = None

        if decision == "approved":
            subject = "✅ Appeal Approved — Account Restored"
            html = f"""
            <div style="font-family:-apple-system,sans-serif;max-width:600px;margin:0 auto;padding:40px 20px;background:#0a0a0f;color:#e2e8f0;border-radius:16px;border:1px solid #1e1e2e">
              <div style="text-align:center;margin-bottom:24px">
                <span style="font-size:1.2rem;font-weight:700;color:#00ffa3">✅ Appeal Approved</span>
              </div>
              <p style="color:#94a3b8;font-size:0.9rem;line-height:1.7">
                Dear User,<br><br>
                After reviewing your appeal, we have decided to <strong style="color:#00ffa3">restore your account</strong>.
                Your original plan and credits have been reinstated.
              </p>
              <div style="background:rgba(0,255,163,0.1);border:1px solid rgba(0,255,163,0.3);border-radius:12px;padding:20px;margin:20px 0">
                <p style="color:#00ffa3;font-weight:700;margin:0 0 8px;font-size:0.85rem">ADMIN RESPONSE:</p>
                <p style="color:#94a3b8;font-size:0.85rem;margin:0;line-height:1.6">{admin_response or 'Your appeal has been approved.'}</p>
              </div>
              <p style="color:#64748b;font-size:0.8rem;margin-top:24px">
                Please note that future violations may result in permanent suspension.
                <br><br>
                <b>Please find the official Appeal Decision Notice attached to this email for your records.</b>
                <br><br>
                — The Surefact Administration Team
              </p>
            </div>
            """
        else:
            subject = "❌ Appeal Denied — Suspension Remains"
            html = f"""
            <div style="font-family:-apple-system,sans-serif;max-width:600px;margin:0 auto;padding:40px 20px;background:#0a0a0f;color:#e2e8f0;border-radius:16px;border:1px solid #1e1e2e">
              <div style="text-align:center;margin-bottom:24px">
                <span style="font-size:1.2rem;font-weight:700;color:#ef4444">❌ Appeal Denied</span>
              </div>
              <p style="color:#94a3b8;font-size:0.9rem;line-height:1.7">
                Dear User,<br><br>
                After reviewing your appeal, we have decided to <strong style="color:#ef4444">uphold the suspension</strong>.
                Your account will be automatically restored when the suspension period expires.
              </p>
              <div style="background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);border-radius:12px;padding:20px;margin:20px 0">
                <p style="color:#ef4444;font-weight:700;margin:0 0 8px;font-size:0.85rem">ADMIN RESPONSE:</p>
                <p style="color:#94a3b8;font-size:0.85rem;margin:0;line-height:1.6">{admin_response or 'Your appeal has been denied.'}</p>
              </div>
              <p style="color:#64748b;font-size:0.8rem;margin-top:24px">
                The suspension will expire as originally scheduled. No further appeals can be submitted for this ban.
                <br><br>
                <b>Please find the official Appeal Decision Notice attached to this email for your records.</b>
                <br><br>
                — The Surefact Administration Team
              </p>
            </div>
            """

        if pdf_bytes:
            asyncio.create_task(send_email_with_attachment(
                appeal["email"], subject, html, pdf_bytes,
                f"Appeal_Decision_{now.strftime('%Y%m%d')}.pdf"
            ))
        else:
            asyncio.create_task(send_custom_email(appeal["email"], subject, html))
    except Exception as e:
        print(f"⚠️ Appeal notification email failed: {e}")
    return {
        "success": True,
        "message": f"Appeal {decision} for {appeal['email']}.",
        "decision": decision
    }
