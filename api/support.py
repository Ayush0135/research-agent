import os
import asyncio
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel, EmailStr
from api.deps import get_current_user
from db.supabase_client import get_supabase_client

router = APIRouter(tags=["Support"])

# --- Models ---
class TicketCreate(BaseModel):
    category: str
    subject: str
    description: str

class NotificationUpdate(BaseModel):
    is_read: bool

# --- User Endpoints ---

@router.post("/tickets")
async def create_ticket(ticket: TicketCreate, user: dict = Depends(get_current_user)):
    """Users submit support/refund/content complaints."""
    try:
        def _insert():
            sb = get_supabase_client()
            return sb.rpc("create_support_ticket_v1", {
                "target_user_id": user["id"],
                "target_email": user["email"],
                "target_category": ticket.category,
                "target_subject": ticket.subject,
                "target_description": ticket.description
            }).execute()
        
        await asyncio.to_thread(_insert)
        return {"status": "success", "message": "Ticket submitted successfully."}
    except Exception as e:
        print(f"Error creating ticket: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit ticket.")

@router.get("/notifications")
async def get_notifications(user: dict = Depends(get_current_user)):
    """Users fetch their notifications."""
    try:
        def _fetch():
            sb = get_supabase_client()
            return sb.rpc("get_user_notifications_v1", {
                "target_user_id": user["id"]
            }).execute().data or []
        
        data = await asyncio.to_thread(_fetch)
        return {"notifications": data}
    except Exception as e:
        print(f"Error fetching notifications: {e}")
        return {"notifications": []}

@router.patch("/notifications/{notif_id}")
async def update_notification(notif_id: str, update: NotificationUpdate, user: dict = Depends(get_current_user)):
    """Mark notification as read."""
    try:
        def _update():
            sb = get_supabase_client()
            return sb.rpc("mark_notification_read_v1", {
                "target_notif_id": notif_id,
                "target_user_id": user["id"],
                "is_read_status": update.is_read
            }).execute()
        
        await asyncio.to_thread(_update)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
