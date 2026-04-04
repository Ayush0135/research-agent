"""
Generate Service — Deep Content Edition

Features:
  - Smart Priority: Uses Groq 70B for deep formats (Research/Reports), HF for fast ones.
  - Multi-Token Budget: Allows up to 4,096 tokens of output (approx. 3,000 words).
  - Anti-Truncate Instructions: Explicitly commands verbosity per section.
"""
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Model Config ──────────────────────────────────────────────────────────────

HF_MODELS = [
    "Qwen/Qwen2.5-7B-Instruct",
    "meta-llama/Meta-Llama-3-8B-Instruct",
    "mistralai/Mistral-Nemo-Instruct-2407",
]

GROQ_MODELS = [
    "llama-3.3-70b-versatile",   # Primary for Elaborate Content
    "llama-3.1-8b-instant",
    "mixtral-8x7b-32768",
]

HF_API_BASE = "https://router.huggingface.co/v1/chat/completions"

# ── Elaborate Token Budgets ───────────────────────────────────────────────────
# (max_input, max_output)
# IMPORTANT: Groq counts (input_tokens + max_tokens) towards the TPM limit.
# For free `on_demand` tier, limit is 6000 TPM. So max_input + max_output MUST be < 6000.
MODEL_BUDGETS = {
    "llama-3.3-70b-versatile": (2800, 1000), 
    "llama-3.1-8b-instant": (2800, 1000),    
    "mixtral-8x7b-32768": (2800, 1000),      
    "Qwen/Qwen2.5-7B-Instruct": (3000, 1000),
    "meta-llama/Meta-Llama-3-8B-Instruct": (3000, 1000),
    "mistralai/Mistral-Nemo-Instruct-2407": (3000, 1000),
    "default": (2500, 1000),
}

def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)

def _trim_context(parts: list[str], max_input: int, overhead: int) -> str:
    budget = max_input - overhead
    selected, used = [], 0
    for part in parts:
        toks = _estimate_tokens(part)
        if used + toks > budget:
            chars = (budget - used) * 4
            if chars > 400:
                selected.append(part[:chars] + "\n...[trimmed context]")
            break
        selected.append(part)
        used += toks
    return ("\n\n" + "─" * 40 + "\n\n").join(selected)

# ── Exhaustive Format Instructions ───────────────────────────────────────────

FORMAT_PROMPTS = {
    "detailed report": (
        "Write an EXHAUSTIVE, IN-DEPTH research report. Each section must be at least 3-4 paragraphs long.\n"
        "# [Title]\n## 1. Executive Summary\n## 2. Comprehensive Background\n"
        "## 3. Detailed Findings (at least 5 detailed subsections with specific data)\n"
        "### 📊 Key Statistics & Trends (Include a detailed table or Mermaid chart)\n"
        "## 4. Deep Analysis & Strategic Implications\n## 5. Challenges & Roadblocks\n"
        "## 6. Future Projections (data-backed)\n## 7. Extended Conclusion\n## 8. References\n"
        "USE MAXIMUM VERBOSITY. Provide deep explanations. Cite [Source X] for every claim."
    ),
    "research paper": (
        "Develop a MASSIVE, MULTI-PAGE ACADEMIC DISSERTATION. Aim for maximal depth.\n"
        "CRITICAL INFLUENCE: YOU MUST WRITE AT LEAST 2500 WORDS. EXPAND EVERY SECTION HEAVILY. DO NOT SKIM.\n"
        "# [Title]\n**Abstract** (Detailed, multi-paragraph explanation of scope)\n"
        "## 1. Introduction (Historical context, Problem Statement, Methodology used. Minimum 3 paragraphs.)\n"
        "## 2. Comprehensive Literature Review (Group findings by theme, compare sources at length)\n"
        "## 3. Results & Quantitative Analysis (Highlight vast statistics, cite [Source X] deeply)\n"
        "## 4. Nuanced Discussion (Interpretations, broader impact, secondary effects, paradigm shifts explored)\n"
        "## 5. Critical Limitations & Scope Gaps (In-depth analysis of what is unknown or constrained)\n"
        "## 6. Formal Conclusions (numbered, highly evidence-based, paragraph-length points)\n## 7. Detailed References\n"
        "Formal academic tone. DO NOT TRUNCATE ANY SECTION. WRITE EXHAUSTIVELY."
    ),
    "comparison table": (
        "Generate a DETAILED COMPARISON. Must include multiple tables and deep analysis.\n"
        "# Comparison: [Topic]\n## 1. Landscape Overview\n"
        "## 2. Feature Comparison Matrix (Mandatory pipe-table, at least 10 rows)\n"
        "| Feature | Option A | Option B | Option C |\n|---|---|---|---|\n"
        "## 3. Pros & Cons Analysis (Deep dive per option)\n"
        "## 4. Cost/Performance Analysis Table\n"
        "## 5. Technical Verdict & Best Use Cases\n"
        "## 6. References\n"
        "Every table cell must contain specific data, not yes/no."
    ),
    "bullet point summary": (
        "Generate a MASSIVE BULLET SUMMARY. Group by deep themes.\n"
        "# Comprehensive Summary: [Topic]\n## 🎯 Key Takeaways (Top 8)\n"
        "## 📊 Extensive Facts & Stats Dashboard\n## ✅ Positives & Opportunities\n"
        "## ❌ Critical Risks & Threats\n## 📈 Market/Scientific Trends\n"
        "## 💡 Experts Insights\n## 🔮 Future Scenarios\n## Sources\n"
        "Every bullet should be a full, detailed sentence with a citation."
    ),
}

DEFAULT_FORMAT_PROMPT = (
    "Generate a highly detailed, elaborate response. Each section must be thorough. "
    "Do not provide brief summaries; provide deep analysis. Cite [Source X] always."
)

# ── Core LLM caller ───────────────────────────────────────────────────────────

import json

async def _call_llm_stream(client: httpx.AsyncClient, endpoint: str, key: str, model: str, messages: list, max_tokens: int):
    payload = {"model": model, "messages": messages, "temperature": 0.35, "max_tokens": max_tokens, "stream": True}
    provider = "Groq" if "groq" in endpoint else "HF"
    
    async with client.stream("POST", endpoint, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=payload, timeout=150.0) as resp:
        if resp.status_code == 429:
            raise Exception(f"RATE_LIMIT: 429 rate-limited on '{model}'")
        if resp.status_code != 200:
            err = await resp.aread()
            raise Exception(f"API Error ({provider}): {err.decode('utf-8')[:200]}")
            
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str.strip() == "[DONE]": break
                try:
                    data = json.loads(data_str)
                    if "choices" in data and len(data["choices"]) > 0:
                        delta = data["choices"][0].get("delta", {})
                        if "content" in delta and delta["content"]:
                            yield delta["content"]
                except json.JSONDecodeError:
                    pass

# ── Load Balancer ───────────────────────────────────────────────────────────
import time
import random
from dataclasses import dataclass, field

@dataclass
class LLMNode:
    provider: str
    endpoint: str
    key: str
    model: str
    is_deep: bool  # True for 70B+ models
    priority: int  # Lower is better
    failure_count: int = 0
    cool_down_until: float = 0.0

class LLMLoadBalancer:
    def __init__(self, nodes: list[LLMNode]):
        self.nodes = nodes

    def get_best_node(self, is_deep_format: bool) -> LLMNode:
        now = time.time()
        # Filter nodes that are not in cool-down
        available = [n for n in self.nodes if n.cool_down_until < now and n.key]
        
        if not available:
            # Emergency: Reset cool-downs if everything is locked
            for n in self.nodes: n.cool_down_until = 0
            available = [n for n in self.nodes if n.key]

        # Prioritize based on format depth and then weight/priority
        # For deep formats, we want is_deep=True nodes first.
        # Otherwise, we prefer fast nodes (is_deep=False).
        
        def _score(n: LLMNode):
            # Base score is priority
            score = n.priority * 10
            # Penalty if it doesn't match the depth requirement
            if is_deep_format and not n.is_deep: score += 100
            if not is_deep_format and n.is_deep: score += 50
            # Random jitter for load balancing among equal nodes
            return score + random.uniform(0, 5)

        available.sort(key=_score)
        return available[0]

    def report_failure(self, node: LLMNode, is_rate_limit: bool):
        node.failure_count += 1
        # If rate limited, cool down for 60s. If other error, 30s.
        penalty = 60 if is_rate_limit else 30
        node.cool_down_until = time.time() + penalty
        print(f"⚠️ Node {node.model} reported failure. Cooling down for {penalty}s.")

# Initialize Global Balancer
def _init_balancer():
    HF_KEY    = os.getenv("HF_API_KEY", "")
    GROQ_KEY  = os.getenv("GROQ_API_KEY", os.getenv("LLM_API_KEY", ""))
    GROQ_URL  = os.getenv("LLM_API_URL", "https://api.groq.com/openai/v1/chat/completions")

    nodes = [
        # Groq 70B (Primary for Deep)
        LLMNode("Groq", GROQ_URL, GROQ_KEY, "llama-3.3-70b-versatile", True, 1),
        # Mixtral (Middle Ground)
        LLMNode("Groq", GROQ_URL, GROQ_KEY, "mixtral-8x7b-32768", True, 2),
        # Fast Nodes
        LLMNode("Groq", GROQ_URL, GROQ_KEY, "llama-3.1-8b-instant", False, 3),
        LLMNode("HF", HF_API_BASE, HF_KEY, "Qwen/Qwen2.5-7B-Instruct", False, 4),
        LLMNode("HF", HF_API_BASE, HF_KEY, "meta-llama/Meta-Llama-3-8B-Instruct", False, 5),
    ]
    return LLMLoadBalancer(nodes)

_BALANCER = None

def get_balancer():
    global _BALANCER
    if _BALANCER is None:
        _BALANCER = _init_balancer()
    return _BALANCER

# ── Global Client ────────────────────────────────────────────────────────────
_ASYNC_CLIENT = None

def get_async_client():
    global _ASYNC_CLIENT
    if _ASYNC_CLIENT is None:
        _ASYNC_CLIENT = httpx.AsyncClient(timeout=150.0, limits=httpx.Limits(max_connections=20, max_keepalive_connections=5))
    return _ASYNC_CLIENT

# ── Config Cache ─────────────────────────────────────────────────────────────
_CONFIG_CACHE = {"data": {}, "expiry": 0}

async def _get_admin_config():
    """Fetches global platform configuration from database with 5-min caching."""
    import time
    if _CONFIG_CACHE["expiry"] > time.time():
        return _CONFIG_CACHE["data"]
        
    try:
        from db.supabase_client import get_supabase_client
        import asyncio
        sb = get_supabase_client()
        res = await asyncio.to_thread(lambda: sb.table("platform_config").select("*").execute())
        config_data = {item['config_key']: item['config_value'] for item in res.data}
        _CONFIG_CACHE["data"] = config_data
        _CONFIG_CACHE["expiry"] = time.time() + 300 # 5 min cache
        return config_data
    except Exception as e:
        print(f"Error fetching admin config: {e}")
        return _CONFIG_CACHE["data"] or {}

# ── Main entry point ──────────────────────────────────────────────────────────

async def generate_report(query: str, format_type: str, ranked_chunks: list[dict], images: list[dict] = None):
    if not ranked_chunks: 
        yield "No verifiable sources found."
        return

    # Load Admin Overrides
    config = await _get_admin_config()
    admin_instr = config.get('system_instruction', '')
    allow_images = config.get('image_generation', True)

    fmt_key   = format_type.lower().strip()
    fmt_instr = FORMAT_PROMPTS.get(fmt_key, DEFAULT_FORMAT_PROMPT)
    is_deep_format = fmt_key in ["research paper", "detailed report"]

    # Context Construction
    context_parts = [f"[Source {i+1}] {c.get('title','Unknown')}\nURL: {c.get('url','')}\n{c.get('text','')}" for i, c in enumerate(ranked_chunks[:12])]
    prompt_overhead = _estimate_tokens(fmt_instr) + _estimate_tokens(query) + 200

    balancer = get_balancer()
    client = get_async_client()
    
    max_retries = 3
    for attempt in range(max_retries):
        node = balancer.get_best_node(is_deep_format)
        if not node: break

        try:
            max_in, max_out = MODEL_BUDGETS.get(node.model, MODEL_BUDGETS["default"])
            trimmed_ctx     = _trim_context(context_parts, max_in, prompt_overhead)
            
            system_prompt = (
                f"ADMIN RULE: {admin_instr}\n\n"
                f"Format: '{format_type}'. MODE: EXHAUSTIVE VERBOSITY.\n\n"
                f"{fmt_instr}\n\n"
                "RULES:\n"
                "- WRITE A MASSIVE AMOUNT OF TEXT.\n"
                "- Every claim must have an inline [Source X] citation.\n"
                "- 📊 Use Mermaid (```mermaid) for diagrams.\n"
                "- 📈 Always include a detailed markdown table.\n"
            )
            
            if allow_images:
                system_prompt += "\n- 🖼️ Prioritize images from the section below. Embed with `![Source-Context Image](URL)`."

            img_section = "\n\n=== ATTACHED IMAGES ===\n"
            if images and allow_images:
                for img in images:
                    img_section += f"- Title: {img['title']}\n  URL: {img['url']}\n"
            else:
                img_section += "(No images available)"

            user_prompt = f"Research Query: {query}\n\n{img_section}\n\n=== VERIFIED SOURCE DATA ===\n{trimmed_ctx}\n\nGenerate '{format_type}' now."
            
            yield {"type": "alert", "content": f"⚖️ [Load Balancer] Routing to {node.provider} ({node.model})..."}
            
            stream_gen = _call_llm_stream(client, node.endpoint, node.key, node.model, [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}], max_tokens=max_out)
            
            has_yielded = False
            async for chunk in stream_gen:
                has_yielded = True
                yield chunk
            
            if has_yielded:
                return

        except Exception as e:
            err_str = str(e)
            is_rate_limit = "RATE_LIMIT" in err_str or "429" in err_str
            balancer.report_failure(node, is_rate_limit)
            yield {"type": "alert", "content": f"⚠️ {node.model} failed, load balancer re-routing..."}
            continue

    yield "⚠️ Generation failed after load balancing retries."
