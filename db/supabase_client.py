import os
from supabase import create_client, Client, ClientOptions
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# ── Resilient Client Strategy ───────────────────────────────────────────────
# We use a custom timeout to prevent "Server disconnected" on long-running tasks.
_options = ClientOptions(
    postgrest_client_timeout=15, 
    storage_client_timeout=15,
    schema="public"
)

supabase: Client = None

if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY, options=_options)

def _create_fresh_client() -> Client:
    """Create a brand-new Supabase client (kills stale HTTP/2 connections)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise Exception("Supabase credentials missing in .env")
    return create_client(SUPABASE_URL, SUPABASE_KEY, options=_options)

def get_supabase_client() -> Client:
    global supabase
    if not supabase:
        supabase = _create_fresh_client()
    return supabase

def refresh_supabase_client() -> Client:
    """Force-refresh the client when ConnectionTerminated errors occur."""
    global supabase
    print("🔄 Refreshing Supabase client (stale HTTP/2 connection detected)...")
    supabase = _create_fresh_client()
    return supabase

def with_supabase_retry(func):
    """
    Decorator/Wrapper to handle Supabase connection issues (HTTP/2 termination).
    If a connection error occurs, it refreshes the client and retries once.
    """
    def wrapper(*args, **kwargs):
        from httpx import RemoteProtocolError
        max_retries = 2
        last_error = None
        
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                err_str = str(e).lower()
                is_conn_error = any(x in err_str for x in ["connectionterminated", "remoteprotocolerror", "handshake_timeout", "read timeout"])
                
                if is_conn_error and attempt < max_retries - 1:
                    refresh_supabase_client()
                    # Re-bind the first argument if it's 'sb' or similar? 
                    # Actually, most of our functions call get_supabase_client() inside the thread,
                    # so refreshing the global 'supabase' is enough.
                    continue
                raise e
    return wrapper

async def with_supabase_retry_async(coro_func):
    """Async version of the retry wrapper for direct async calls if needed."""
    async def wrapper(*args, **kwargs):
        # Similar logic for async
        max_retries = 2
        for attempt in range(max_retries):
            try:
                return await coro_func(*args, **kwargs)
            except Exception as e:
                if attempt < max_retries - 1 and "ConnectionTerminated" in str(e):
                    refresh_supabase_client()
                    continue
                raise e
    return wrapper

