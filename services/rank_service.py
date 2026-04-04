import asyncio
import numpy as np
from typing import List, Dict

from services.embedding_service import generate_embedding, generate_embeddings_batch
from db.supabase_client import get_supabase_client

async def rank_and_store_chunks(chunks: List[Dict], query: str) -> List[Dict]:
    """
    1. Generates embeddings using the Hugging Face model
    2. Ranks by cosine similarity against the original search query
    3. Trims to max 3 chunks per source and 5 active sources total
    4. Fire-and-forgets a background task to store embeddings in Supabase
    """
    if not chunks:
        return []

    # 1. Embed query and chunks concurrently (though chunks usually dominate)
    query_emb_task = generate_embedding(query)
    texts = [c["text"] for c in chunks]
    chunk_embs_task = generate_embeddings_batch(texts)
    
    query_emb, chunk_embs = await asyncio.gather(query_emb_task, chunk_embs_task)
    
    def _rank_logic():
        # Numpy arrays for fast cosine similarity
        q_vec = np.array(query_emb)
        q_norm = np.linalg.norm(q_vec)
        
        ranked = []
        for i, chunk in enumerate(chunks):
            c_vec = np.array(chunk_embs[i])
            c_norm = np.linalg.norm(c_vec)
            if q_norm == 0 or c_norm == 0:
                sim = 0.0
            else:
                sim = np.dot(q_vec, c_vec) / (q_norm * c_norm)
                
            chunk["embedding"] = chunk_embs[i]
            chunk["similarity"] = float(sim)
            ranked.append(chunk)
            
        # Prioritize similarity score mixed with earlier verification score
        ranked.sort(key=lambda x: (x["similarity"] + x.get("verification_score", 0)*0.2), reverse=True)
        
        # Enforce limits: Max 10 sources, Max 4 chunks per source
        source_counts = {}
        sources_seen = set()
        final_top_k = []
        
        for r_chunk in ranked:
            url = r_chunk["url"]
            # Enforce 10 source limit
            if len(sources_seen) >= 10 and url not in sources_seen:
                continue
                
            source_counts[url] = source_counts.get(url, 0) + 1
            if source_counts[url] <= 4:
                sources_seen.add(url)
                final_top_k.append(r_chunk)
                
        return final_top_k
        
    top_k_chunks = await asyncio.to_thread(_rank_logic)
    
    # 2. Store in Supabase Fire-and-Forget
    asyncio.create_task(_store_in_supabase(query, top_k_chunks))
    
    return top_k_chunks

async def _store_in_supabase(query: str, chunks: List[Dict]):
    """ We wrap saving to Supabase to prevent slowing the response to the user. """
    try:
        sb = get_supabase_client()
        # Ensure we have client initialized before sending
        if sb:
            records = [{
                "query_text": query,
                "url": c["url"],
                "content_chunk": c["text"],
                "embedding": c["embedding"], # pgvector requires [1,2,3...] list
                "similarity_score": c["similarity"]
            } for c in chunks]
            # Store ranked chunks and vectors in Superbase's pgvector table securely
            def _insert():
                sb.table("document_chunks").insert(records).execute()
                
            await asyncio.to_thread(_insert)
    except Exception as e:
        print(f"Skipped saving to Supabase: {e}")
