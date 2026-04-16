import os
import httpx
import json
import asyncio
import time
import random
from dataclasses import dataclass
from typing import AsyncGenerator, Optional
from dotenv import load_dotenv

load_dotenv()

# ── Model Config & Budgets ───────────────────────────────────────────────────

HF_API_BASE = "https://router.huggingface.co/v1/chat/completions"
GROQ_URL = os.getenv("LLM_API_URL", "https://api.groq.com/openai/v1/chat/completions")

MODEL_BUDGETS = {
    "llama-3.3-70b-versatile": (4000, 2048),
    "llama-3.1-8b-instant": (4000, 2048),
    "mixtral-8x7b-32768": (4000, 2048),
    "default": (3500, 1500),
}

# ── Humanization & Tone Instructions ──────────────────────────────────────────

HUMAN_PROMPT = (
    "TONE: High-end consulting, analytical, and deeply engaging. Avoid generic AI patterns.\n"
    "BURSTINESS: Use a mix of short, punchy insights and long, explanatory paragraphs.\n"
    "FORBIDDEN: 'landscape', 'important to note', 'in conclusion', 'delve', 'overall'.\n"
    "CITE: Use [Source X] inline for every claim. No exceptions.\n"
    "IMAGES: Randomly embed 1-2 images from the provided list using `![Source Image](URL)`."
)

@dataclass
class LLMNode:
    provider: str
    endpoint: str
    key: str
    model: str
    is_deep: bool
    priority: int
    cool_down_until: float = 0.0

class LLMLoadBalancer:
    def __init__(self, nodes: list[LLMNode]):
        self.nodes = nodes

    def get_best_node(self, is_deep: bool) -> LLMNode:
        now = time.time()
        available = [n for n in self.nodes if n.cool_down_until < now and n.key]
        if not available:
            for n in self.nodes: n.cool_down_until = 0
            available = [n for n in self.nodes if n.key]
        
        available.sort(key=lambda n: (n.priority + (100 if is_deep and not n.is_deep else 0) + random.uniform(0, 5)))
        return available[0]

    def report_failure(self, node: LLMNode, is_rate_limit: bool):
        node.cool_down_until = time.time() + (60 if is_rate_limit else 20)

def _get_nodes():
    HF_KEY = os.getenv("HF_API_KEY", "")
    GROQ_KEY = os.getenv("GROQ_API_KEY", os.getenv("LLM_API_KEY", ""))
    return [
        LLMNode("Groq", GROQ_URL, GROQ_KEY, "llama-3.3-70b-versatile", True, 1),
        LLMNode("Groq", GROQ_URL, GROQ_KEY, "mixtral-8x7b-32768", True, 2),
        LLMNode("Groq", GROQ_URL, GROQ_KEY, "llama-3.1-8b-instant", False, 3),
        LLMNode("HF", HF_API_BASE, HF_KEY, "Qwen/Qwen2.5-7B-Instruct", False, 4),
    ]

BALANCER = LLMLoadBalancer(_get_nodes())
_CLIENT = None

def get_client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.AsyncClient(timeout=120.0, limits=httpx.Limits(max_connections=50))
    return _CLIENT

# ── Core Helpers ─────────────────────────────────────────────────────────────

async def _call_llm(messages: list, node: LLMNode, max_tokens: int) -> Optional[str]:
    try:
        resp = await get_client().post(
            node.endpoint, 
            headers={"Authorization": f"Bearer {node.key}"},
            json={"model": node.model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.4},
            timeout=45.0
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
    except Exception: pass
    return None

async def _call_llm_stream(messages: list, node: LLMNode, max_tokens: int) -> AsyncGenerator[str, None]:
    try:
        async with get_client().stream(
            "POST", node.endpoint,
            headers={"Authorization": f"Bearer {node.key}"},
            json={"model": node.model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.4, "stream": True},
            timeout=90.0
        ) as resp:
            if resp.status_code != 200: return
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    token = line[6:]
                    if token.strip() == "[DONE]": break
                    try:
                        data = json.loads(token)
                        if "choices" in data:
                            content = data["choices"][0].get("delta", {}).get("content")
                            if content: yield content
                    except Exception: pass
    except Exception: pass

# ── Main Generator ────────────────────────────────────────────────────────────

async def generate_report(query: str, format_type: str, ranked_chunks: list[dict], images: list[dict] = None):
    """Sequential Generation for 15,000+ words with Humanized Tone & Images."""
    if not ranked_chunks:
        yield "Missing research data."
        return

    # Step 1: Generate Outline
    yield {"type": "alert", "content": "📍 Phase 1: Drafting the massive Research Outline..."}
    node = BALANCER.get_best_node(True)
    outline_prompt = f"Topic: {query}\nTask: Generate a 15-point DETAILED sequence of chapter headings for a 15,000-word report. Use 'SECTION X: [Title]' format. No intro/outro."
    outline_raw = await _call_llm([{"role": "user", "content": outline_prompt}], node, 1000)
    
    sections = [s.strip() for s in (outline_raw or "").split("\n") if "SECTION" in s.upper()]
    if not sections: sections = [f"SECTION {i+1}: Analytical Depth Part {i+1}" for i in range(10)]

    yield f"# {query.title()}\n\n"
    
    # Step 2: Loop through sections (Sequential Multiplexing)
    context = "\n".join([f"[Source {i+1}] {c.get('title','')} ({c.get('url','')})\n{c.get('text','')[:1500]}" for i, c in enumerate(ranked_chunks[:15])])
    
    for i, section_title in enumerate(sections):
        yield {"type": "alert", "content": f"🖋️ Phase 2: Writing {section_title}..."}
        
        node = BALANCER.get_best_node(True)
        # Select relevant images for this section
        img_refs = ""
        if images:
            idx = (i * 2) % len(images)
            curr = images[idx : idx+2]
            img_refs = "\n\n=== IMAGES TO EMBED ===\n" + "\n".join([f"- {m['title']}: {m['url']}" for m in curr])

        system_msg = f"GENERATE AN EXTREMELY DETAILED 1,000-WORD CHAPTER.\n{HUMAN_PROMPT}\nSECTION: {section_title}"
        user_msg = f"Query: {query}\nChapter: {section_title}\n{img_refs}\n\n=== CONTEXT ===\n{context}\n\nWrite Section Now:"
        
        yield f"\n\n## {section_title}\n"
        async for chunk in _call_llm_stream([{"role": "system", "content": system_msg}, {"role": "user", "content": user_msg}], node, 2048):
            yield chunk

    # Step 3: Bibliography
    yield "\n\n# References & Bibliography\n" + "\n".join([f"- [{c.get('title','Link')}]({c.get('url','')})" for c in ranked_chunks[:15]])
