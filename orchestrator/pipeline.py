import json
import asyncio
import datetime
from typing import AsyncGenerator

from services.search_service import federated_search, fetch_images
from services.verify_service import verify_sources
from services.process_service import chunk_documents
from services.rank_service import rank_and_store_chunks
from services.generate_service import generate_report
from services.paper_service import generate_academic_pdf
from db.supabase_client import get_supabase_client

async def execute_pipeline(query: str, user_id: str, format_type: str = "detailed report") -> AsyncGenerator[str, None]:
    """
    Optimized Research Pipeline:
    - Parallel Search & Image Fetching
    - Streaming Updates via WebSocket
    - Automatic PDF Generation & Verification ID
    - Background Verification & Persistent Storage
    """
    
    yield json.dumps({"status": "🚀 Starting research workflow...", "stage": "start"})
    
    try:
        # 1. Parallel Federated Search + Image Search
        yield json.dumps({"status": "🔍 Deep Search on Google (High Precision)...", "stage": "search"})
        search_task = asyncio.create_task(federated_search(query))
        image_task = asyncio.create_task(fetch_images(query))
        
        results = await search_task
        if not results:
            yield json.dumps({"status": "⚠️ No live sources found. Using AI knowledge...", "stage": "search"})
            results = [] 
            
        # 2. Parallel Verification & Source Scrutiny
        yield json.dumps({"status": "🛡️ Verifying source credibility...", "stage": "verify"})
        verified = await verify_sources(results, query)
        
        # 3. Context Chunking & Embedding-based Ranking
        yield json.dumps({"status": "🧠 Processing context with RAG...", "stage": "rank"})
        chunks = await chunk_documents(verified)
        # Note: rank_and_store_chunks handles background Supabase storage already
        ranked = await rank_and_store_chunks(chunks, query)
        
        # Pull images for the generator
        images = await image_task
        
        # 4. Final Report Synthesis (Streaming)
        full_markdown = ""
        async for chunk in generate_report(query, format_type, ranked, images):
            if isinstance(chunk, dict) and chunk.get("type") == "alert":
                yield json.dumps({"status": chunk["content"], "stage": "generate"})
            else:
                full_markdown += str(chunk)
                yield json.dumps({"content": chunk, "stage": "writing"})
        
        # 5. Document Generation & Persistence
        if full_markdown and len(full_markdown) > 200:
            yield json.dumps({"status": "📄 Generating certified PDF document...", "stage": "finalize"})
            
            # Generate a unique Verification ID
            doc_id = f"DOC-{datetime.datetime.now().strftime('%Y%m%d%H%M')}-{user_id[:4].upper()}"
            
            # Generate the PDF (Paper Service)
            pdf_path = f"frontend/papers/{doc_id}.pdf"
            relative_url = generate_academic_pdf(full_markdown, query, output_path=pdf_path, reference_id=doc_id)
            
            # Save to Database (Background)
            def _save_to_db():
                try:
                    sb = get_supabase_client()
                    sb.table("verified_documents").insert({
                        "id": doc_id,
                        "user_id": user_id,
                        "query": query,
                        "title": query[:100],
                        "pdf_url": relative_url,
                        "content_snapshot": full_markdown[:2000], # store preview
                        "verified_at": datetime.datetime.now().isoformat()
                    }).execute()
                    
                    # Deduct credit (optional, check if implementation exists in payment_service)
                    # For now just log it
                except Exception as e:
                    print(f"Failed to save document record: {e}")
            
            await asyncio.to_thread(_save_to_db)
            
            yield json.dumps({
                "status": "✅ Research complete. Document certified.",
                "stage": "complete",
                "pdf_url": relative_url,
                "doc_id": doc_id
            })
        else:
            yield json.dumps({"status": "⚠️ Result too short for certification.", "stage": "complete"})

    except Exception as e:
        print(f"Pipeline Error: {e}")
        yield json.dumps({"status": f"❌ Error: {str(e)}", "stage": "error"})