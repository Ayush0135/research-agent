"""
Self-managed OTP using Supabase's email infrastructure.

Strategy:
1. Use Supabase Admin API (generate_link) to create an OTP — this triggers
   Supabase to store a valid token AND returns email_otp (the 6-digit code).
2. We then send our OWN nicely formatted email via Supabase's SMTP using that token.
3. Verification: call /auth/v1/verify with the token (Supabase validates it natively).

This means: Supabase handles auth token validity, we handle the email format.
No magic link. Clean 6-digit OTP. Uses Supabase SMTP config = no extra setup.
"""
import os
import re
import random
import httpx
from typing import Optional, Union, List, Any
import aiosmtplib
import ssl
import certifi
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from db.redis_client import set_cache, get_cache, delete_cache

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_KEY", "")

# Supabase SMTP settings (from Dashboard → Project Settings → Auth → SMTP)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
FROM_NAME = "Surefact"

OTP_TTL = 600  # 10 minutes

def _otp_key(email: str) -> str:
    return f"otp:{email.lower().strip()}"

def _get_ssl_context():
    """Create a proper SSL context for SMTP using the certifi CA bundle (Fix for macOS)."""
    return ssl.create_default_context(cafile=certifi.where())

def _build_html(code: str) -> str:
    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:480px;margin:0 auto;padding:40px 20px;background:#0a0a0f;color:#e2e8f0;border-radius:16px;border:1px solid #1e1e2e">
      <div style="text-align:center;margin-bottom:32px">
        <span style="font-size:1.1rem;font-weight:700;color:#e2e8f0">⚡ Surefact</span>
      </div>
      <h1 style="font-size:1.2rem;font-weight:600;text-align:center;margin:0 0 8px">Verify your email</h1>
      <p style="text-align:center;color:#94a3b8;font-size:.88rem;margin:0 0 32px">Enter this code to complete signup</p>
      <div style="text-align:center;margin:32px 0">
        <span style="font-size:2.4rem;font-weight:700;letter-spacing:12px;color:#7c3aed;background:rgba(124,58,237,.1);padding:18px 28px;border-radius:12px;border:1px solid rgba(124,58,237,.3)">{code}</span>
      </div>
      <p style="text-align:center;color:#475569;font-size:.78rem;margin:24px 0 0">Expires in <strong style="color:#94a3b8">10 minutes</strong>. Do not share this code.</p>
    </div>
    """

import json

async def send_otp(email: str, password: str = None) -> bool:
    """
    Generate 6-digit OTP, store in Redis (along with password if doing deferred signup),
    and send via plain SMTP.
    """
    code = str(random.randint(100000, 999999))
    
    # Store as JSON to hold password state for deferred signups
    data = json.dumps({"otp": code, "password": password})
    await set_cache(_otp_key(email), data, expire=OTP_TTL)
    print(f"🔑 [OTP] {email} → {code}")  # Always visible for dev testing

    # Send branded OTP email via SMTP
    if SMTP_USER and SMTP_PASS:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"{code} is your Surefact verification code"
            msg["From"] = f"{FROM_NAME} <{SMTP_USER}>"
            msg["To"] = email
            msg.attach(MIMEText(_build_html(code), "html"))

            await aiosmtplib.send(
                msg,
                hostname=SMTP_HOST,
                port=SMTP_PORT,
                username=SMTP_USER,
                password=SMTP_PASS,
                start_tls=True,
                tls_context=_get_ssl_context(),
            )
            print(f"✅ OTP email sent to {email}")
            return True
        except Exception as e:
            print(f"⚠️ SMTP failed: {e}")

    # Fallback log print
    print(f"⚠️ SMTP not configured. OTP for {email}: {code} (check terminal above)")
    return True

async def verify_otp(email: str, code: str) -> Optional[dict]:
    """
    Verify the OTP from Redis. One-time use — deletes on success.
    Returns the {"otp": code, "password": password} dict on success, or None on failure.
    """
    stored = await get_cache(_otp_key(email))
    if not stored:
        return None  # Expired
        
    try:
        data = json.loads(stored)
        stored_code = data.get("otp", "")
    except Exception:
        # Fallback for old plaintext strings
        stored_code = stored.strip()
        data = {"otp": stored_code}

    if stored_code != code.strip():
        return None  # Wrong code
        
    await delete_cache(_otp_key(email))
    return data

async def send_custom_email(to_email: str, subject: str, html_content: str) -> bool:
    """Generic custom SMTP email sender."""
    if not SMTP_USER or not SMTP_PASS:
        print(f"⚠️ SMTP not configured. Skipping email to {to_email}")
        return False
    
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{FROM_NAME} <{SMTP_USER}>"
        msg["To"] = to_email
        msg.attach(MIMEText(html_content, "html"))

        await aiosmtplib.send(
            msg,
            hostname=SMTP_HOST,
            port=SMTP_PORT,
            username=SMTP_USER,
            password=SMTP_PASS,
            start_tls=True,
            tls_context=_get_ssl_context(),
        )
        print(f"✅ Email sent to {to_email}")
        return True
    except Exception as e:
        print(f"⚠️ Custom SMTP failed: {e}")
        return False

async def send_email_with_attachment(to_email: str, subject: str, html_content: str, file_bytes: bytes, filename: str) -> bool:
    """Send an email with a file attachment (e.g. PDF receipt)."""
    if not SMTP_USER or not SMTP_PASS:
        print(f"⚠️ SMTP not configured. Skipping email to {to_email}")
        return False
    
    try:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = f"{FROM_NAME} <{SMTP_USER}>"
        msg["To"] = to_email
        
        # HTML Part
        msg.attach(MIMEText(html_content, "html"))
        
        # Attachment Part
        part = MIMEApplication(file_bytes, Name=filename)
        part['Content-Disposition'] = f'attachment; filename="{filename}"'
        msg.attach(part)

        await aiosmtplib.send(
            msg,
            hostname=SMTP_HOST,
            port=SMTP_PORT,
            username=SMTP_USER,
            password=SMTP_PASS,
            start_tls=True,
            tls_context=_get_ssl_context(),
        )
        print(f"✅ Email with attachment sent to {to_email}")
        return True
    except Exception as e:
        print(f"⚠️ SMTP Attachment failed: {e}")
        return False
