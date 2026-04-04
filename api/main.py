import json
import asyncio
from typing import List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel
from pathlib import Path

from orchestrator.pipeline import execute_pipeline
from services.payment_service import (
    create_payment_order, 
    verify_payment_by_order, 
    get_user_profile,
    calculate_refundable_amount,
    submit_refund_request,
    get_user_payments,
    PLAN_CONFIG
)
from api import auth, admin, support
from api.deps import get_current_user, get_ws_user
from db.sqlite_client import get_history, delete_history_item

app = FastAPI(title="Surefact API", version="1.1.0")
app.include_router(auth.router, tags=["Authentication"])
app.include_router(admin.router, prefix="/admin-api", tags=["Admin Portal"])
app.include_router(support.router, prefix="/support", tags=["Support"])

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=1000)
templates = Jinja2Templates(directory="frontend")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

# ── Rate Limiting Middleware ───────────────────────────────────────────────────
import time
from db.redis_client import redis_client
from fastapi.responses import JSONResponse

async def get_cached_config(key: str, default: int) -> int:
    try:
        val = await redis_client.get(f"config:{key}")
        if val: return int(val)
        from db.supabase_client import get_supabase_client
        sb = get_supabase_client()
        res = await asyncio.to_thread(lambda: sb.table("platform_config").select("config_value").eq("config_key", key).execute())
        if res.data:
            val = int(res.data[0]["config_value"])
            await redis_client.set(f"config:{key}", str(val), ex=60)  # cache logic config for 60s
            return val
    except Exception: pass
    return default

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path.startswith("/static"):
        return await call_next(request)
        
    client_ip = request.client.host if request.client else "unknown"
    key = f"rl_http:{client_ip}:{int(time.time() // 60)}"
    
    try:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.set(key, 1, ex=120)
            
        limit = await get_cached_config("http_rate_limit", 120)
        if count > limit:
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded. Try again in a minute."})
    except Exception:
        pass # Fail open if cache fails
        
    return await call_next(request)

# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health(): return {"status": "ok"}

@app.get("/")
async def serve_landing():
    # In a real app, this would be a marketing landing page or redirect
    return FileResponse(str(FRONTEND_DIR / "login.html"))

@app.get("/login")
async def serve_login():
    path = FRONTEND_DIR / "login.html"
    return FileResponse(str(path)) if path.exists() else {"message": "Login UI not found"}

@app.get("/dashboard")
async def serve_dashboard():
    path = FRONTEND_DIR / "dashboard.html"
    return FileResponse(str(path)) if path.exists() else {"message": "Dashboard UI not found"}

@app.get("/admin")
async def serve_admin_ui():
    path = FRONTEND_DIR / "admin.html"
    return FileResponse(str(path)) if path.exists() else {"message": "Admin UI not found"}

@app.get("/history")
async def history(user: dict = Depends(get_current_user)):
    return await get_history(user["id"])

@app.delete("/history/{item_id}")
async def delete_hist(item_id: int, user: dict = Depends(get_current_user)):
    await delete_history_item(item_id, user["id"])
    return {"status": "deleted"}

class VerifyReq(BaseModel):
    order_id: str
    utr_number: str

class PaymentOrderReq(BaseModel):
    plan: str
    plan_credits: Optional[int] = None
    plan_amount: Optional[float] = None

@app.post("/payment/create-order")
async def create_order_route(req: PaymentOrderReq, user: dict = Depends(get_current_user)):
    res = await create_payment_order(user["id"], user["email"], req.plan,
                                      override_amount=req.plan_amount,
                                      override_credits=req.plan_credits)
    if not res.get("success"): raise HTTPException(400, res.get("error"))
    return res

@app.post("/payment/verify")

async def verify_payment(req: VerifyReq, user: dict = Depends(get_current_user)):
    res = await verify_payment_by_order(user["id"], req.order_id, req.utr_number)
    if not res.get("success"): raise HTTPException(400, res.get("error"))
    return res

@app.get("/verify/{order_id}")
@app.get("/verify")
async def verify_receipt_portal(request: Request, order_id: str = None):
    """ Public route to verify a receipt authenticity by scanning QR or manual entry. """
    from db.supabase_client import get_supabase_client
    import asyncio
    
    data = None
    if order_id:
        def _fetch():
            sb = get_supabase_client()
            if order_id.startswith("DOC-"):
                doc_res = sb.table("verified_documents").select("*").eq("id", order_id).execute()
                if not doc_res.data: return None
                doc_data = doc_res.data[0]
                user_res = sb.table("users").select("plan, is_active").eq("id", doc_data["user_id"]).execute()
                if user_res.data:
                    doc_data["plan"] = user_res.data[0]["plan"]
                    doc_data["is_active"] = user_res.data[0]["is_active"]
                doc_data["type"] = "document"
                return doc_data
            else:
                res = sb.table("pending_payments").select("*").eq("order_id", order_id).eq("status", "approved").execute()
                if res.data:
                    data = res.data[0]
                    data["type"] = "receipt"
                    return data
                return None
        data = await asyncio.to_thread(_fetch)
    
    if data:
        ctx = {
            "request": request, "verified": True, "error": False,
            "type": data.get("type", "receipt"),
            "plan": str(data.get("plan", "N/A")),
            "email": str(data.get("email", "N/A")),
            "date": str(data.get("verified_at") or data.get("created_at", "N/A"))[:10],
            "order_id": order_id,
            "title": data.get("title", ""),
            "pdf_url": data.get("pdf_url", ""),
            "is_active": data.get("is_active", True)
        }
    elif order_id:
        ctx = {"request": request, "verified": False, "error": True, "order_id": order_id}
    else:
        ctx = {"request": request, "verified": False, "error": False, "order_id": ""}
        
    return templates.TemplateResponse("verify.html", ctx)

# ── Subscription Cancellation & Refunds ──────────────────────────────────────


@app.get("/payment/refund-status")
async def get_refund_calc(user: dict = Depends(get_current_user)):
    res = await calculate_refundable_amount(user["id"])
    if not res.get("success"): raise HTTPException(400, res.get("error"))
    # Add pricing breakdown for UI transparency
    res["price_per_credit"] = round(res["amount_paid"] / PLAN_CONFIG[res["plan"]]["credits"], 2)
    res["usage_deduction"] = round(res["amount_paid"] - res["estimated_refund"], 2)
    return res

@app.get("/payment/history")
async def get_pay_history(user: dict = Depends(get_current_user)):
    res = await get_user_payments(user["id"])
    return {"history": res}

class RefundReq(BaseModel):
    bank_details: dict
    survey_results: Optional[dict] = None

@app.post("/payment/cancel-subscription")
async def cancel_sub(req: RefundReq, user: dict = Depends(get_current_user)):
    res = await submit_refund_request(user["id"], user["email"], req.bank_details, req.survey_results)
    if not res.get("success"): raise HTTPException(400, res.get("error"))
    return res

# ── WebSocket Orchestrator ──────────────────────────────────────────────────

@app.websocket("/ws/research")
async def ws_research(websocket: WebSocket):
    await websocket.accept()
    user = await get_ws_user(websocket)
    if not user:
        await websocket.send_json({"status": "Unauthorized", "stage": "error"})
        await websocket.close()
        return

    # WebSocket Rate Limiting (Prevent abuse of AI generation)
    import time
    from db.redis_client import redis_client
    key = f"rl_ws:{user['id']}:{int(time.time() // 60)}"
    try:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.set(key, 1, ex=120)
            
        limit = await get_cached_config("ws_rate_limit", 8)
        if count > limit: # dynamic max limit
            await websocket.send_json({"status": f"Rate limit exceeded ({limit}/min). Please slow down.", "stage": "error"})
            await websocket.close()
            return
    except Exception:
        pass
        await websocket.close(4001); return

    try:
        while True:
            data = await websocket.receive_json()
            query, fmt = data.get("query", ""), data.get("format", "detailed report")
            if not query.strip(): continue

            async for update in execute_pipeline(query, user["id"], fmt):
                await websocket.send_text(update)
    except WebSocketDisconnect: pass
    except Exception as e: print(f"WS Error: {e}")
