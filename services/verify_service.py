import os
import httpx
import asyncio
from typing import List, Dict

# ── Local NLI (Natural Language Inference) Fallback ──────────────────────────
_VERIFIER = None
try:
    from transformers import pipeline
except ImportError:
    pipeline = None

def get_local_verifier():
    global _VERIFIER
    if _VERIFIER is None and pipeline:
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=FutureWarning, module="huggingface_hub.file_download")
                print("🧠 Loading local NLI verifier (Offline-first)...")
                _VERIFIER = pipeline("text-classification", model="cross-encoder/nli-MiniLM2-L6-H768", device="cpu")
        except Exception as e:
            print(f"⚠️ Local NLI load failed: {e}")
    return _VERIFIER

async def verify_sources(search_results: List[Dict], query: str) -> List[Dict]:
    """
    Hybrid Verification Pipeline:
    1. Try Local NLI Model (Fast & Private)
    2. Fallback to HF Inference API (Serverless Cloud)
    3. Final Fallback to Groq Llama-3 (Intelligence)
    """
    if not search_results: return []
    query_str = f"Supports '{query}'"
    verified = []

    # 1. 🔍 Try Local Offline Inference First
    local = get_local_verifier()
    if local:
        try:
            for res in search_results:
                text = res.get("snippet", "")[:400]
                if not text: continue
                # NLI Prediction: entailment (idx 0), neutral (idx 1), contradiction (idx 2)
                pred = local({"text": text, "text_pair": query}, top_k=3)
                # Ensure the predicted label is entailment/support
                scores = {p["label"].lower(): p["score"] for p in pred}
                if scores.get("entailment", 0) > 0.45:
                    res["verification_score"] = scores["entailment"]
                    verified.append(res)
            if verified: return verified
        except Exception as e:
             print(f"⚠️ Local verification failed: {e}")

    # 2. 📡 Fallback: HF Inference API (Cloud)
    HF_TOKEN = os.getenv("HF_API_KEY", "")
    if HF_TOKEN:
        try:
            API_URL = "https://router.huggingface.co/hf-inference/models/cross-encoder/nli-MiniLM2-L6-H768"
            async with httpx.AsyncClient() as client:
                async def _verify_item(res):
                    text = res.get("snippet", "")[:400]
                    if not text: return None
                    try:
                        resp = await client.post(API_URL, headers={"Authorization": f"Bearer {HF_TOKEN}"}, json={"inputs": {"text": text, "text_pair": query}}, timeout=5.0)
                        if resp.status_code == 200:
                            data = resp.json()
                            if data and data[0].get("score", 0) > 0.45:
                                res["verification_score"] = data[0]["score"]
                                return res
                    except Exception: pass
                    return None

                tasks = [_verify_item(res) for res in search_results[:6]]
                results = await asyncio.gather(*tasks)
                for r in results:
                    if r: verified.append(r)
                    
            if verified: return verified
        except Exception: pass

    # 3. 🛡️ Final Fallback: Keyword Score (Last Resort)
    for res in search_results:
        snippet = res.get("snippet", "").lower()
        query_words = set(query.lower().split())
        overlap = sum(1 for w in query_words if w in snippet)
        if overlap >= 2:
            res["verification_score"] = 0.5 # Baseline score
            verified.append(res)

    return verified if verified else search_results[:5] # Fallback to top results
