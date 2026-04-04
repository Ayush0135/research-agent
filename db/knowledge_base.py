"""
Knowledge Base — Self-Learning Memory for the Research Orchestrator

Every completed research is:
1. Embedded and stored in Supabase (vector search later)
2. Used to improve results for future similar queries
3. Tracks domain-level source quality scores

The orchestrator gets smarter with every query it processes.
"""
import asyncio
import json
from urllib.parse import urlparse
from services.embedding_service import generate_embedding
from db.supabase_client import get_supabase_client, with_supabase_retry


# ── Retrieve: similar past research ─────────────────────────────────────────

async def recall_similar_research(query: str, user_id: str, threshold: float = 0.82) -> list[dict]:
    """
    Searches the memory for similar past queries using cosine similarity.
    Returns up to 3 past results that are semantically close to the current query.
    Used to augment the LLM context with prior knowledge.
    """
    try:
        query_emb = await generate_embedding(query)

        @with_supabase_retry
        def _query():
            sb = get_supabase_client()
            result = sb.rpc("match_research_memory", {
                "query_embedding": query_emb,
                "similarity_threshold": threshold,
                "match_count": 3,
                "filter_user_id": None  # search across all users for public knowledge
            }).execute()
            return result.data or []

        memories = await asyncio.to_thread(_query)

        # Increment access_count for recalled memories
        if memories:
            ids = [m["id"] for m in memories]
            @with_supabase_retry
            def _update():
                sb = get_supabase_client()
                for mid in ids:
                    sb.table("research_memory").update({"access_count": sb.table("research_memory")
                        .select("access_count").eq("id", mid).execute().data[0]["access_count"] + 1
                    }).eq("id", mid).execute()
            asyncio.create_task(asyncio.to_thread(_update))

        return memories

        return memories

    except Exception as e:
        print(f"⚠️ Memory recall failed (non-critical): {e}")
        return []

async def recall_knowledge_chunks(query: str, threshold: float = 0.75, limit: int = 8) -> list[dict]:
    """
    Experimental: Searches for specific granular document chunks (facts) 
    instead of just report summaries. Provides higher fidelity context.
    """
    try:
        query_emb = await generate_embedding(query)
        @with_supabase_retry
        def _query():
            sb = get_supabase_client()
            return sb.rpc("match_document_chunks", {
                "query_embedding": query_emb,
                "match_threshold": threshold,
                "match_count": limit
            }).execute().data or []
        return await asyncio.to_thread(_query)
    except Exception as e:
        print(f"⚠️ Chunk recall failed: {e}")
        return []


# ── Store: save completed research to memory ─────────────────────────────────

async def memorize_research(
    user_id: str,
    query: str,
    format_type: str,
    result: str,
    ranked_sources: list[dict],
    quality_score: float = 0.0
):
    """
    Stores a completed research result into vector memory.
    Both the query and result are embedded so future retrieval
    can match either similar questions or similar content.
    """
    try:
        # Generate embeddings for query and result in parallel
        query_emb, result_emb = await asyncio.gather(
            generate_embedding(query),
            generate_embedding(result[:2000])  # truncate for embedding model
        )

        # Extract source metadata
        sources_meta = [
            {
                "url": s.get("url", ""),
                "domain": _domain(s.get("url", "")),
                "score": s.get("similarity", 0.0)
            }
            for s in ranked_sources[:5]
        ]

        @with_supabase_retry
        def _insert():
            sb = get_supabase_client()
            response = sb.table("research_memory").insert({
                "user_id": user_id,
                "query": query,
                "format": format_type,
                "result": result,
                "query_embedding": query_emb,
                "result_embedding": result_emb,
                "sources": sources_meta,
                "quality_score": quality_score,
            }).execute()
            
            if response.data and ranked_sources:
                mem_id = response.data[0]["id"]
                # Save top 5 chunks into the granular memory
                chunk_payloads = []
                for s in ranked_sources[:5]:
                    chunk_payloads.append({
                        "query_text": query,
                        "url": s.get("url", ""),
                        "content_chunk": s.get("text", "")[:3000],
                        "embedding": s.get("embedding", query_emb), # Fallback if not Ranked
                        "similarity_score": s.get("similarity", 0.0),
                        "memory_id": mem_id,
                        "user_id": user_id
                    })
                if chunk_payloads:
                    sb.table("document_chunks").insert(chunk_payloads).execute()

        await asyncio.to_thread(_insert)
        print(f"✅ Memorized research: '{query[:50]}...' (quality={quality_score:.2f})")

        # Update source quality scores from this research
        asyncio.create_task(_update_source_quality(sources_meta, quality_score))

    except Exception as e:
        print(f"⚠️ Memory storage failed (non-critical): {e}")


# ── Source Quality: domain-level trust learning ───────────────────────────────

async def _update_source_quality(sources: list[dict], result_quality: float):
    """
    Tracks which domains consistently produce high-quality research material.
    Domains that appear in high-quality results get higher trust scores.
    """
    try:
        @with_supabase_retry
        def _upsert():
            sb = get_supabase_client()
            for src in sources:
                domain = src.get("domain", "")
                if not domain:
                    continue
                # Check if domain exists
                existing = sb.table("source_quality").select("*").eq("domain", domain).execute().data
                if existing:
                    row = existing[0]
                    new_total = row["total_uses"] + 1
                    new_high  = row["high_quality"] + (1 if result_quality > 0.75 else 0)
                    # Bayesian update to trust score
                    new_trust = (row["trust_score"] * row["total_uses"] + result_quality) / new_total
                    sb.table("source_quality").update({
                        "trust_score": round(new_trust, 4),
                        "total_uses": new_total,
                        "high_quality": new_high,
                        "last_seen": "now()"
                    }).eq("domain", domain).execute()
                else:
                    sb.table("source_quality").insert({
                        "domain": domain,
                        "trust_score": result_quality,
                        "total_uses": 1,
                        "high_quality": 1 if result_quality > 0.75 else 0
                    }).execute()

        await asyncio.to_thread(_upsert)
    except Exception as e:
        print(f"⚠️ Source quality update failed: {e}")


async def get_trusted_domains(top_n: int = 10) -> list[dict]:
    """Returns top trusted source domains sorted by trust score * usage."""
    try:
        @with_supabase_retry
        def _fetch():
            sb = get_supabase_client()
            return sb.table("source_quality") \
                .select("domain, trust_score, total_uses, high_quality") \
                .order("trust_score", desc=True) \
                .limit(top_n) \
                .execute().data or []
        return await asyncio.to_thread(_fetch)
    except Exception:
        return []


# ── Quality Scoring ───────────────────────────────────────────────────────────

def compute_quality_score(ranked_chunks: list[dict], result: str) -> float:
    """
    Heuristic quality score (0.0 - 1.0) based on:
    - Average similarity of top ranked chunks
    - Result length (more detailed = better up to a point)
    - Number of sources used
    """
    if not ranked_chunks or not result:
        return 0.0

    avg_sim  = sum(c.get("similarity", 0) for c in ranked_chunks) / len(ranked_chunks)
    len_score = min(len(result) / 3000, 1.0)       # normalize to 3000 chars = max
    src_score = min(len(ranked_chunks) / 5, 1.0)   # 5+ sources = max

    return round(avg_sim * 0.5 + len_score * 0.3 + src_score * 0.2, 4)


def build_memory_context(memories: list[dict]) -> str:
    """
    Formats recalled memories into a structured context block
    injected into the LLM prompt as prior knowledge.
    """
    if not memories:
        return ""
    lines = ["=== PRIOR KNOWLEDGE FROM MEMORY ==="]
    for i, m in enumerate(memories):
        sim_pct = int(m.get("similarity", 0) * 100)
        lines.append(f"\n[Memory {i+1}] (Similarity: {sim_pct}%, Quality: {m.get('quality_score',0):.2f})")
        lines.append(f"Query: {m['query']}")
        lines.append(f"Summary: {str(m.get('result',''))[:600]}...")
    lines.append("=== END PRIOR KNOWLEDGE ===\n")
    return "\n".join(lines)


# ── Utility ───────────────────────────────────────────────────────────────────

def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""
