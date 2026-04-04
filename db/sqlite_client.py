import asyncio
from datetime import datetime
from db.supabase_client import get_supabase_client

# This module now uses Supabase instead of a local SQLite file 
# to ensure data persistence on platforms like Render/Vercel.

async def save_research(user_id: str, query: str, format_type: str, result: str, download_url: str = None):
    """Saves a research result via RPC (Security Definer) to bypass RLS."""
    try:
        def _insert():
            sb = get_supabase_client()
            # Calling the security-definer RPC to ensure history is tracked for all users
            sb.rpc("save_research_audit", {
                "target_user_id": user_id,
                "query_text": query,
                "format_text": format_type,
                "result_text": str(result)
            }).execute()
        
        await asyncio.to_thread(_insert)
    except Exception as e:
        print(f"⚠️ Failed to save history to Supabase RPC: {e}")

async def get_history(user_id: str, limit: int = 20) -> list[dict]:
    """Fetch recent research history for a user from Supabase."""
    try:
        def _fetch():
            sb = get_supabase_client()
            response = sb.table("research_history") \
                .select("*") \
                .eq("user_id", user_id) \
                .order("created_at", desc=True) \
                .limit(limit) \
                .execute()
            return response.data or []
        
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        print(f"⚠️ Failed to fetch history from Supabase: {e}")
        return []

async def delete_history_item(item_id: int, user_id: str):
    """Delete a specific history item from Supabase."""
    try:
        def _delete():
            sb = get_supabase_client()
            sb.table("research_history") \
                .delete() \
                .eq("id", item_id) \
                .eq("user_id", user_id) \
                .execute()
        
        await asyncio.to_thread(_delete)
    except Exception as e:
        print(f"⚠️ Failed to delete history from Supabase: {e}")
