from fastapi import APIRouter, Depends, HTTPException, status, Request
from pydantic import BaseModel
from typing import List, Optional
import asyncio
import uuid
from db.supabase_client import get_supabase_client
from db.redis_client import set_cache, get_cache
from services.otp_service import send_otp, verify_otp, send_email_with_attachment
from services.receipt_service import generate_refund_receipt
from datetime import datetime
import os
import httpx
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from services.payment_service import (
    get_pending_verifications_admin, 
    approve_payment_admin, 
    reject_payment_admin
)
from services.suspension_service import (
    suspend_user, check_suspension, revoke_suspension,
    get_all_suspensions, SUSPENSION_REASONS
)

router = APIRouter(tags=["Admin Dashboard"])
bearer_scheme = HTTPBearer()

# ── REQUEST SCHEMAS ──
class EmailRequest(BaseModel):
    email: str

class VerifyRequest(BaseModel):
    email: str
    token: str

class ConfigUpdateRequest(BaseModel):
    configs: dict # {key: value}

# ── ADMIN AUTH MIDDLEWARE (Decoupled from User Auth) ──
async def require_admin(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    """Validates the custom Admin Session Token from Redis."""
    token = credentials.credentials
    email = await get_cache(f"admin_session:{token}")
    
    if not email:
        raise HTTPException(status_code=403, detail="Forbidden: Admin session expired or invalid.")
    
    return {"email": email, "is_admin": True}

# (Admin login routes moved to auth.py)




# ── ADMIN DASHBOARD ROUTES ──
@router.get("/dashboard-stats")
async def get_dashboard_stats(admin: dict = Depends(require_admin)):
    try:
        def _fetch_stats():
            sb = get_supabase_client()
            
            # Using RPCs which are security-definer (bypass RLS)
            # Full history (already has ORDER and LIMIT 100 in RPC)
            total_history_res = sb.rpc("get_all_research_history").execute()
            history = total_history_res.data or []
            
            # Use the same data for 'recent_queries'
            recent_queries = history[:50]
            
            # Use the dedicated RPC for users
            users_res = sb.rpc("get_all_users_with_credits").execute()
            enriched_users = users_res.data or []

            # Trends: Daily queries (last 7 days)
            from datetime import datetime, timedelta
            week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
            
            # For trends, we'll extract from history since RPC limited to 100
            daily_counts = {}
            for q in history:
                if q.get('created_at'):
                    day = q['created_at'][:10]  # YYYY-MM-DD
                    if day >= week_ago[:10]:
                        daily_counts[day] = daily_counts.get(day, 0) + 1

            return {
                "stats": {
                    "total_users": len(enriched_users),
                    "total_queries": len(history),
                    "active_premium": sum(1 for p in enriched_users if p.get("plan") and str(p.get("plan")).lower() != "free"),
                    "total_credits_in_system": sum(int(p.get("credits_remaining", 0) or 0) for p in enriched_users)
                },
                "recent_queries": recent_queries,
                "trends": {
                    "daily_queries": daily_counts,
                    "top_queries": [q['query'][:50] + '...' for q in recent_queries[:10] if q.get('query')],
                    "new_users_today": len([u for u in enriched_users if u.get('created_at', '').startswith(datetime.utcnow().isoformat()[:10])])
                }
            }
        
        return await asyncio.to_thread(_fetch_stats)
    except Exception as e:
        print(f"Error fetching dashboard stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/users")
async def get_all_users(admin: dict = Depends(require_admin)):
    """Fetches all users with their credits/history using bypass RPC."""
    try:
        sb = get_supabase_client()
        # Use the bypass RPC to get all users + profiles + history_count at once
        res = await asyncio.to_thread(lambda: sb.rpc("get_all_users_with_credits").execute())
        data = res.data or []
        
        print(f"DEBUG users endpoint: {len(data)} users fetched via RPC (w/ counts)")
        return {"users": data}
    except Exception as e:
        print(f"Error enumerating users: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ── SUPPORT & COMPLIANCE ──
@router.get("/support/tickets")
async def get_support_tickets(admin: dict = Depends(require_admin)):
    """Admins fetch all support tickets using bypass RPC."""
    try:
        sb = get_supabase_client()
        res = await asyncio.to_thread(lambda: sb.rpc("get_all_support_tickets").execute())
        return {"tickets": res.data or []}
    except Exception as e:
        print(f"Error fetching tickets: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class SupportReply(BaseModel):
    response: str

@router.post("/support/tickets/{ticket_id}/reply")
async def reply_to_ticket(ticket_id: str, reply: SupportReply, admin: dict = Depends(require_admin)):
    """Reply to a ticket, notify the user, and send an email."""
    try:
        sb = get_supabase_client()
        # Fetch ticket info first to get user_id and email using bypass RPC
        ticket_res = await asyncio.to_thread(lambda: sb.rpc("get_support_ticket_admin_v1", {"target_id": ticket_id}).execute())
        if not ticket_res.data:
            raise HTTPException(status_code=404, detail="Ticket not found")
        
        ticket = ticket_res.data[0]
        user_id = ticket["user_id"]
        user_email = ticket["email"]

        # 1. Update DB and Create internal notification (via RPC)
        await asyncio.to_thread(lambda: sb.rpc("resolve_ticket_with_response", {
            "ticket_id": ticket_id,
            "response_text": reply.response,
            "u_id": user_id
        }).execute())

        # 2. Send External Email Response
        from services.otp_service import send_custom_email # reuse existing SMTP logic
        email_content = f"""
        <h2>Support Update: {ticket['subject']}</h2>
        <p>Hello,</p>
        <p>Our support team has responded to your inquiry regarding {ticket['category']}:</p>
        <div style="padding: 15px; background: #f4f4f4; border-color: #ddd; border-style: solid; border-width: 1px; border-radius: 5px; color: #333;">
            {reply.response}
        </div>
        <p>Best regards,<br>The Surefact Team</p>
        """
        asyncio.create_task(send_custom_email(user_email, f"RE: {ticket['subject']}", email_content))

        return {"status": "success", "message": "Reply sent and ticket resolved."}
    except Exception as e:
        print(f"Error replying to ticket: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/users/{user_id}")
async def delete_user(user_id: str, admin: dict = Depends(require_admin)):
    """Deletes a user from the platform. Requires Service Key for Auth deletion."""
    SUPABASE_URL = os.getenv("SUPABASE_URL", "")
    # Use Service Key for admin operations
    SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
    
    if SERVICE_KEY:
        async with httpx.AsyncClient() as client:
            try:
                # 1. Delete from Auth Users (Admin API)
                resp = await client.delete(
                    f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}",
                    headers={"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}
                )
                if resp.status_code not in [200, 204]:
                    print(f"⚠️ Auth deletion failed for {user_id}: {resp.text}")
            except Exception as e:
                print(f"⚠️ Auth deletion error: {e}")
    else:
        print("⚠️ SUPABASE_SERVICE_KEY not found. Skipping Auth deletion, proceeding with DB wipe.")
            
    # 2. Database Wipe (Force delete from all tables)
    sb = get_supabase_client()
    def _delete_db():
        # Cascade should handle this, but we'll be thorough
        sb.table("research_history").delete().eq("user_id", user_id).execute()
        sb.table("research_memory").delete().eq("user_id", user_id).execute()
        sb.table("document_chunks").delete().eq("user_id", user_id).execute()
        sb.table("user_credits").delete().eq("user_id", user_id).execute()
        sb.table("users").delete().eq("id", user_id).execute()
        
    try:
        await asyncio.to_thread(_delete_db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database wipe failed: {str(e)}")
        
    return {"message": "User eradicated from platform database."}

@router.get("/users/{user_id}/history")
async def get_user_history_admin(user_id: str, admin: dict = Depends(require_admin), limit: int = 100):
    """Fetches full history for a specific user using bypass RPC."""
    print(f"DEBUG: Fetching history for user_id={user_id}, limit={limit}")
    try:
        sb = get_supabase_client()
        # Use the security definer RPC to bypass RLS
        res = await asyncio.to_thread(lambda: sb.rpc("get_user_history_admin", {"target_user_id": user_id}).execute())
        
        history_data = res.data or []
        # Apply the limit locally since it's already fetched
        history_data = history_data[:limit]
        
        print(f"DEBUG: Found {len(history_data)} history items for {user_id} via RPC")
        return {"history": history_data, "limit": limit, "debug_count": len(history_data)}
    except Exception as e:
        print(f"Error fetching user history: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ── REFUND MANAGEMENT ──
@router.get("/refunds")
async def list_refunds(admin: dict = Depends(require_admin)):
    print(f"DEBUG: Admin {admin['email']} requesting all refunds...")
    try:
        sb = get_supabase_client()
        res = await asyncio.to_thread(lambda: sb.rpc("get_all_refund_requests").execute())
        return {"refunds": res.data or []}
    except Exception as e:
        print(f"Error listing refunds: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ── PAYMENT VERIFICATION (Counter-Fraud) ──
@router.get("/payments/pending")
async def list_pending_payments(admin: dict = Depends(require_admin)):
    """ List all payments submitted for verification """
    res = await get_pending_verifications_admin()
    return {"payments": res}

@router.get("/payments/approved")
async def list_approved_payments(admin: dict = Depends(require_admin)):
    """ List all approved/certified payments for the Certificates dashboard """
    try:
        sb = get_supabase_client()
        res = await asyncio.to_thread(lambda: sb.table("pending_payments").select("*").eq("status", "approved").order("verified_at", desc=True).execute())
        return {"payments": res.data or []}
    except Exception as e:
        print(f"Error fetching approved payments: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/receipts/{order_id}/download")
async def download_receipt_admin(order_id: str, admin: dict = Depends(require_admin)):
    """ Generate and download a professional PDF for a specific order on-demand. """
    try:
        sb = get_supabase_client()
        res = await asyncio.to_thread(lambda: sb.table("pending_payments").select("*").eq("order_id", order_id).execute())
        if not res.data: raise HTTPException(404, "Order not found")
        
        ord = res.data[0]
        from services.receipt_service import generate_payment_receipt
        
        receipt_data = {
            "email": ord["email"],
            "plan": ord["plan"],
            "amount": float(ord["amount"]),
            "transaction_id": ord["order_id"],
            "credits": ord.get("credits_granted", 0),
            "date": ord.get("verified_at", "N/A")[:10]
        }
        
        pdf_bytes = generate_payment_receipt(receipt_data)
        
        from fastapi.responses import StreamingResponse
        import io
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=Receipt_{order_id}.pdf"}
        )
    except Exception as e:
        print(f"PDF download error: {e}")
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=str(e))

class PaymentApprovalReq(BaseModel):
    order_id: str

class PaymentRejectionReq(BaseModel):
    order_id: str
    reason: Optional[str] = None

@router.post("/payments-approve-v2")
async def approve_payment_v2_route(req: PaymentApprovalReq, admin: dict = Depends(require_admin)):
    """ Award credits using body-based order ID for maximum stability """
    res = await approve_payment_admin(req.order_id, admin["email"])
    if not res.get("success"): raise HTTPException(400, res.get("error"))
    return res

@router.post("/payments-reject-v2")
async def reject_payment_v2_route(req: PaymentRejectionReq, admin: dict = Depends(require_admin)):
    """ Reject payment using body-based order ID for maximum stability """
    res = await reject_payment_admin(req.order_id, req.reason or "Invalid reference")
    if not res.get("success"): raise HTTPException(400, res.get("error"))
    return res

class CompleteRefundReq(BaseModel):
    transaction_id: str

@router.post("/refunds/{refund_id}/complete")
async def complete_refund(refund_id: str, payload: CompleteRefundReq, admin: dict = Depends(require_admin)):
    """Completes refund via a secure RPC that bypasses RLS."""
    try:
        sb = get_supabase_client()
        
        # 1. Update status + Fetch details in 1 Atomic RPC call (SECURITY DEFINER)
        def _execute():
            return sb.rpc("complete_refund_process", {
                "p_refund_id": refund_id,
                "p_transaction_id": payload.transaction_id,
                "p_processed_by": admin["email"]
            }).execute()
            
        res = await asyncio.to_thread(_execute)
        
        if not res.data:
            print(f"DEBUG: RPC returned zero rows for refund_id: {refund_id}")
            raise HTTPException(404, "Refund record not found or update failed")
            
        r = res.data[0] # RPC returns a list since it's SETOF

        # 2. Generate PDF Receipt
        receipt_data = {
            "email": r["email"],
            "plan": r["plan"],
            "refund_amount": float(r["refund_amount"]),
            "transaction_id": payload.transaction_id,
            "credits_returned": r["credits_at_cancellation"],
            "date": datetime.now().strftime('%Y-%m-%d')
        }
        pdf_bytes = generate_refund_receipt(receipt_data)

        # 3. Send Email with Attachment (Async)
        html_content = f"""
        <div style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #eee; border-radius: 10px;">
            <h2 style="color: #00ffa3;">Refund Processed</h2>
            <p>Hello,</p>
            <p>We've successfully processed your refund for the <b>{receipt_data['plan'].upper()}</b> plan.</p>
            <p><b>Refund Amount:</b> INR {receipt_data['refund_amount']}<br>
               <b>Transaction ID:</b> {receipt_data['transaction_id']}</p>
            <p>Please find your official receipt attached to this email.</p>
            <p>Best regards,<br>The Surefact Team</p>
        </div>
        """
        asyncio.create_task(send_email_with_attachment(
            r["email"], 
            f"Refund Receipt - Surefact [{payload.transaction_id}]", 
            html_content, 
            pdf_bytes, 
            f"Surefact_Refund_{payload.transaction_id}.pdf"
        ))

        return {"success": True, "message": "Refund processed, receipt sent."}
    except Exception as e:
        print(f"Error completing refund process: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ── ADMIN REGISTRY MANAGEMENT ──
@router.get("/registry")
async def get_admin_registry(admin: dict = Depends(require_admin)):
    sb = get_supabase_client()
    res = await asyncio.to_thread(lambda: sb.table("admin_users").select("*").order("created_at", desc=True).execute())
    return {"admins": res.data or []}

@router.post("/registry")
async def add_admin_to_registry(payload: EmailRequest, admin: dict = Depends(require_admin)):
    # RESTRICTION: Only the primary admin can add new admins
    PRIMARY_ADMIN = "ayush.kashyap7155@gmail.com"
    if admin["email"] != PRIMARY_ADMIN:
        raise HTTPException(status_code=403, detail=f"Permission Denied: Only {PRIMARY_ADMIN} can grant Security Clearance.")
        
    try:
        sb = get_supabase_client()
        # Check if already exists
        exists = await asyncio.to_thread(lambda: sb.table("admin_users").select("email").eq("email", payload.email).execute())
        if exists.data:
            raise HTTPException(status_code=400, detail="User is already registered as Admin.")
            
        res = await asyncio.to_thread(lambda: sb.table("admin_users").insert({"email": payload.email}).execute())
        return {"message": f"Added {payload.email} to Admin Registry successfully.", "data": res.data}
    except Exception as e:
        print(f"Error adding admin: {e}")
        # Return a safe error message if it wasn't an HTTPException
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail="Internal server error while granting access.")

@router.delete("/registry/{admin_id}")
async def remove_admin_from_registry(admin_id: str, admin: dict = Depends(require_admin)):
    # RESTRICTION: Only the primary admin can revoke admins
    PRIMARY_ADMIN = "ayush.kashyap7155@gmail.com"
    if admin["email"] != PRIMARY_ADMIN:
        raise HTTPException(status_code=403, detail=f"Permission Denied: Only {PRIMARY_ADMIN} can revoke Security Clearance.")

    try:
        sb = get_supabase_client()
        # Prevent deleting oneself
        user_record = await asyncio.to_thread(lambda: sb.table("admin_users").select("email").eq("id", admin_id).execute())
        if user_record.data and user_record.data[0].get("email") == admin["email"]:
            raise HTTPException(status_code=400, detail="You cannot revoke your own Security Access.")
            
        await asyncio.to_thread(lambda: sb.table("admin_users").delete().eq("id", admin_id).execute())
        return {"message": "Admin clearance revoked."}
    except Exception as e:
        print(f"Error revoking admin: {e}")
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail="Internal server error while revoking access.")
@router.get("/config")
async def get_platform_config(admin: dict = Depends(require_admin)):
    """Fetches global platform configuration."""
    try:
        sb = get_supabase_client()
        res = await asyncio.to_thread(lambda: sb.table("platform_config").select("*").execute())
        configs = {item['config_key']: item['config_value'] for item in res.data}
        return configs
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/config")
async def update_platform_config(configs: dict, admin: dict = Depends(require_admin)):
    """Updates one or more global platform configurations."""
    try:
        sb = get_supabase_client()
        from db.redis_client import redis_client
        
        for key, val in configs.items():
             await asyncio.to_thread(lambda k=key, v=val: sb.table("platform_config").upsert({
                "config_key": k,
                "config_value": v
            }, on_conflict="config_key").execute())
             
             # Sync back to cache instantly
             try:
                 await redis_client.set(f"config:{key}", str(val), ex=60)
             except Exception:
                 pass
                 
        return {"success": True}
    except Exception as e:
        print(f"Update error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ── ACCOUNT SUSPENSION MANAGEMENT ────────────────────────────────────────────

class SuspendUserReq(BaseModel):
    user_id: str
    email: str
    reason_code: str
    notes: Optional[str] = None

@router.get("/suspension-reasons")
async def get_suspension_reasons(admin: dict = Depends(require_admin)):
    """Returns available suspension reason codes for the admin UI dropdown."""
    return {code: info["label"] for code, info in SUSPENSION_REASONS.items()}

@router.post("/suspend-user")
async def suspend_user_route(req: SuspendUserReq, admin: dict = Depends(require_admin)):
    """Suspend a user account with a predefined reason code."""
    res = await suspend_user(
        user_id=req.user_id,
        email=req.email,
        reason_code=req.reason_code,
        admin_email=admin["email"],
        notes=req.notes
    )
    if not res.get("success"):
        raise HTTPException(400, res.get("error"))
    return res

@router.get("/suspensions")
async def list_suspensions(admin: dict = Depends(require_admin)):
    """Fetch all suspension records."""
    data = await get_all_suspensions()
    return {"suspensions": data}

@router.post("/suspensions/{suspension_id}/revoke")
async def revoke_suspension_route(suspension_id: str, admin: dict = Depends(require_admin)):
    """Lift a suspension early."""
    res = await revoke_suspension(suspension_id, admin["email"])
    if not res.get("success"):
        raise HTTPException(400, res.get("error"))
    return res

# ── Appeals Management ───────────────────────────────────────────────────────

class ProcessAppealRequest(BaseModel):
    decision: str  # "approved" or "rejected"
    admin_response: str = None

@router.get("/appeals")
async def list_appeals(admin: dict = Depends(require_admin)):
    from services.suspension_service import get_all_appeals
    data = await get_all_appeals()
    return {"appeals": data}

@router.post("/appeals/{appeal_id}/process")
async def process_appeal_route(appeal_id: str, payload: ProcessAppealRequest, admin: dict = Depends(require_admin)):
    from services.suspension_service import process_appeal
    res = await process_appeal(appeal_id, payload.decision, admin["email"], payload.admin_response)
    if not res.get("success"):
        raise HTTPException(400, res.get("error"))
    return res
