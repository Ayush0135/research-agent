import os
# --- macOS Stability Fixes (Prevents libc++abi mutex crash) ---
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

import httpx
import asyncio
import numpy as np
from typing import List, Dict

# --- Local AI (Primary Layer) ---
_MODEL = None
try:
    from sentence_transformers import SentenceTransformer
    # Only load on first use to save initial RAM
except ImportError:
    SentenceTransformer = None

HF_API_URL = "https://router.huggingface.co/hf-inference/models/sentence-transformers/all-MiniLM-L6-v2"
HF_TOKEN = os.getenv("HF_API_KEY", "")

def get_local_model():
    global _MODEL
    if _MODEL is None and SentenceTransformer:
        try:
            import torch
            # Prioritize GPU acceleration: CUDA (NVIDIA) -> MPS (Mac M1/M2/M3) -> CPU
            device = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
            
            import warnings
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=FutureWarning, module="huggingface_hub.file_download")
                print(f"🧠 Loading local embedding model (all-MiniLM-L6-v2) on device: {device}")
                _MODEL = SentenceTransformer("all-MiniLM-L6-v2", device=device)
        except Exception as e:
            print(f"⚠️ Local model load failed: {e}")
    return _MODEL

async def generate_embedding(text: str) -> list[float]:
    """
    Hybrid Embedding Strategy (Token-Saving First):
    0. Redis Cache Hit (FREE — zero compute)
    1. Local Model (Fast & Private)
    2. HF Serverless API (Cloud Fallback)
    3. Keyword Vector (Last Resort)
    """
    from db.redis_client import get_cached_embedding, cache_embedding

    # -- 0. Redis Cache (Zero Cost) --
    cached = await get_cached_embedding(text)
    if cached:
        return cached

    embedding = None

    # -- 1. Local Offline Try --
    local = get_local_model()
    if local:
        try:
            embedding = local.encode(text).tolist()
        except Exception as e:
            print(f"⚠️ Local inference failed: {e}")

    # -- 2. HF API Try --
    if embedding is None and HF_TOKEN:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    HF_API_URL,
                    headers={"Authorization": f"Bearer {HF_TOKEN}"},
                    json={"inputs": text, "options": {"wait_for_model": True}},
                    timeout=10.0
                )
                if resp.status_code == 200:
                    data = resp.json()
                    embedding = data[0] if isinstance(data, list) else data
                else:
                    print(f"⚠️ HF Embedding API error ({resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"⚠️ HF API connection failed: {e}")

    # -- 3. Final Fallback --
    if embedding is None:
        words = set(text.lower().split())
        embedding = [1.0 if len(words) > 0 else 0.0] * 384

    # -- Save to Cache for future calls --
    await cache_embedding(text, embedding)
    return embedding


async def generate_embeddings_batch(texts: List[str]) -> List[List[float]]:
    """Batch version of generate_embedding (Primary Offline-First)."""
    local = get_local_model()
    if local:
        try:
            return local.encode(texts).tolist()
        except: pass
        
    # Sequential fallback for APIs or failure
    tasks = [generate_embedding(t) for t in texts]
    return await asyncio.gather(*tasks)
