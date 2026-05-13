import os
import httpx
import json
import asyncio
import time
import random
import re
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
    "STRUCTURE: Use rich Markdown. Include ### and #### sub-headings, bullet points, and 1-2 tables PER CHAPTER where data permits. Do NOT start by repeating the chapter title.\n"
    "BURSTINESS: Use a mix of short, punchy insights and long, explanatory paragraphs.\n"
    "FORBIDDEN: 'landscape', 'important to note', 'in conclusion', 'delve', 'overall'.\n"
    "CITE: Use [Source X] inline for every claim. No exceptions. Ensure a SPACE before citations.\n"
    "IMAGES: Randomly embed 1-2 images from the provided list using standard markdown: `![Title](URL)`."
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
    yield {"type": "alert", "content": "📍 Phase 1: Designing an authoritative 15,000-word research architecture..."}
    node = BALANCER.get_best_node(True)
    outline_prompt = (
        f"TOPIC: {query}\n"
        "TASK: Generate a professional, academic outline for a 15,000-word research paper.\n"
        "REQUIREMENT: Provide exactly 12-15 descriptive sub-topics that cover the breadth and depth of the topic.\n"
        "FORMAT: Each line must be 'SECTION X: [Title of the Chapter]'.\n"
        "No intro/outro/introductory text."
    )
    outline_raw = await _call_llm([{"role": "user", "content": outline_prompt}], node, 1200)
    
    # Robust parsing: handle various formats the LLM might return
    sections = []
    if outline_raw:
        for line in outline_raw.split("\n"):
            line = line.strip()
            if not line: continue
            # Extract title after 'SECTION X:' or similar
            cleaned = re.sub(r'^SECTION\s*\d+[:\s]*', '', line, flags=re.IGNORECASE).strip()
            if cleaned and len(cleaned) > 5:
                sections.append(cleaned)
    
    if not sections: 
        sections = [
            "Historical Context & Evolution", "Core Methodologies & Paradigms", 
            "Economic Impact & Global Trends", "Socio-Cultural Implications",
            "Technological Disruption & Innovation", "Policy Frameworks & Regulatory Landscape",
            "Case Studies & Empirical Evidence", "Future Projections & Speculative Analysis",
            "Critical Critique & Limitations", "Conclusion & Strategic Recommendations"
        ]

    yield f"# {query.title()}\n\n"
    
    # Step 2: Loop through sections (Sequential Multiplexing)
    full_context = "\n".join([f"[Source {i+1}] {c.get('title','')} ({c.get('url','')})\n{c.get('text','')[:2000]}" for i, c in enumerate(ranked_chunks[:15])])
    
    for i, section_title in enumerate(sections):
        yield {"type": "alert", "content": f"🖋️ Phase 2: Compiling Chapter {i+1}: {section_title}..."}
        
        node = BALANCER.get_best_node(True)
        img_refs = ""
        if images:
            idx = (i * 2) % len(images)
            curr = images[idx : idx+2]
            img_refs = "\n\n=== REFERENCE IMAGES ===\n" + "\n".join([f"- {m['title']}: {m['url']}" for m in curr])

        system_msg = (
            f"You are a Senior Research Analyst. Write an EXTREMELY IN-DEPTH chapter for a major academic publication.\n"
            f"{HUMAN_PROMPT}\n"
            f"GOAL: Produce at least 1,200 words of high-density analysis for this specific section.\n"
            f"DO NOT repeat the section title or use #/## headings at the start of your response."
        )
        user_msg = (
            f"TOPIC: {query}\n"
            f"CHAPTER TITLE: {section_title}\n"
            f"{img_refs}\n\n"
            f"=== RESEARCH CONTEXT ===\n{full_context}\n\n"
            f"Write the complete, detailed chapter now (min. 1,200 words):"
        )
        
        yield f"\n\n## SECTION {i+1}: {section_title}\n"
        async for chunk in _call_llm_stream([{"role": "system", "content": system_msg}, {"role": "user", "content": user_msg}], node, 2500):
            yield chunk

    # Step 3: Bibliography
    yield "\n\n<hr/>\n\n# References & Bibliography\n" + "\n".join([f"- [{c.get('title','Link')}]({c.get('url','')})" for c in ranked_chunks[:20]])
