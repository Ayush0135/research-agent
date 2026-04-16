import json
import asyncio
import datetime
import time
from typing import AsyncGenerator

from services.search_service import federated_search, fetch_images
from services.verify_service import verify_sources
from services.process_service import chunk_documents
from services.rank_service import rank_and_store_chunks
from services.generate_service import generate_report
from services.paper_service import generate_academic_pdf
from services.payment_service import check_credits, deduct_credits
from db.supabase_client import get_supabase_client

async def execute_pipeline(query: str, user_id: str, format_type: str = "detailed report") -> AsyncGenerator[str, None]:
    """
    Optimized Research Pipeline:
    - Parallel Search & Image Fetching
    - Streaming Updates via WebSocket
    - Automatic PDF Generation & Verification ID
    - Background Verification & Persistent Storage
    - Credit Deduction on Completion (as per System Design)
    """
    
    start_time = time.time()
    yield json.dumps({"status": "🚀 Starting research workflow...", "stage": "start"})

    # 0. Credit Check (System Design Enforcement)
    has_credits = await check_credits(user_id)
    if not has_credits:
        yield json.dumps({"status": "❌ Insufficient credits. Please upgrade your plan.", "stage": "error"})
        return
    
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
            
            # Generate the PDF in a background thread to prevent WebSocket hangs
            pdf_path = f"frontend/papers/{doc_id}.pdf"
            relative_url = await asyncio.to_thread(
                generate_academic_pdf, 
                full_markdown, 
                query, 
                output_path=pdf_path, 
                reference_id=doc_id
            )
            
            # Deduct Credit (System Design: Paid Plan -> Deduct Credits)
            try:
                await deduct_credits(user_id)
            except Exception as e:
                print(f"Credit deduction failed: {e}")
            
            # Save to Database (Background)
            def _save_to_db():
                try:
                    sb = get_supabase_client()
                    
                    # 1. Save to verified_documents (Receipt/Certification)
                    sb.table("verified_documents").upsert({
                        "id": doc_id,
                        "user_id": user_id,
                        "title": query[:200],
                        "pdf_url": relative_url
                    }).execute()
                    
                    # 2. Save to research_history (User Dashboard)
                    sb.table("research_history").insert({
                        "user_id": user_id,
                        "query": query,
                        "format": format_type,
                        "result": full_markdown,
                        "download_url": relative_url
                    }).execute()
                    
                except Exception as e:
                    import traceback
                    traceback.print_exc()
            
            await asyncio.to_thread(_save_to_db)
            
            total_time = round(time.time() - start_time, 1)
            yield json.dumps({
                "status": "✅ Research complete. Document certified.",
                "stage": "done",
                "result": full_markdown,
                "download_url": relative_url,
                "doc_id": doc_id,
                "total_time": total_time
            })
        else:
            yield json.dumps({"status": "⚠️ Result too short for certification.", "stage": "complete"})

    except Exception as e:
        print(f"Pipeline Error: {e}")
        yield json.dumps({"status": f"❌ Error: {str(e)}", "stage": "error"})