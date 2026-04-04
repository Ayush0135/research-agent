import os
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
SEARCH_ENGINE_ID = os.getenv("SEARCH_ENGINE_ID", "")

async def _scrape_url(client: httpx.AsyncClient, link: str, title: str, snippet: str) -> dict:
    """Scrape a URL for content, extracting paragraphs, headings, or parsing PDFs for richer context."""
    try:
        is_pdf_url = link.lower().endswith(".pdf")
        page_resp = await client.get(link, timeout=10.0, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (Research Bot)"})
        
        content_type = page_resp.headers.get("Content-Type", "").lower()
        is_pdf = is_pdf_url or "application/pdf" in content_type

        if is_pdf:
            import io
            from pypdf import PdfReader
            pdf_file = io.BytesIO(page_resp.content)
            reader = PdfReader(pdf_file)
            text = []
            for i in range(min(5, len(reader.pages))):
                page_text = reader.pages[i].extract_text()
                if page_text: text.append(page_text)
            body_text = "\n".join(text)
            return {
                "url": link,
                "title": title + " [PDF]",
                "snippet": snippet,
                "content": body_text[:12000],
                "scraped_images": []
            }

        soup = BeautifulSoup(page_resp.text, "html.parser")
        # Extract headings for structure
        headings = [h.get_text(strip=True) for h in soup.find_all(['h1','h2','h3']) if h.get_text(strip=True)]
        # Extract paragraphs
        paragraphs = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 40]
        # Extract list items for bullet-heavy pages
        list_items = [li.get_text(strip=True) for li in soup.find_all('li') if len(li.get_text(strip=True)) > 20]

        heading_text = ' | '.join(headings[:8])
        body_text = ' '.join(paragraphs + list_items)
        combined = f"[Headings: {heading_text}]\n{body_text}" if heading_text else body_text
        
        # Extract images from <img> tags
        import urllib.parse
        scraped_images = []
        for img in soup.find_all('img'):
            src = img.get('src')
            if not src: continue
            
            # Convert relative to absolute
            abs_url = urllib.parse.urljoin(link, src)
            
            # Simple metadata extraction
            alt = img.get('alt', '').strip()
            # If alt is empty, try to get nearby sibling text or skip
            if not alt:
                continue
                
            # Filter obvious small icons/trackers (just heuristic check)
            if 'icon' in src.lower() or 'logo' in src.lower() or 'pixel' in src.lower():
                continue
                
            scraped_images.append({
                "url": abs_url,
                "title": alt or title
            })
            
        return {
            "url": link,
            "title": title,
            "snippet": snippet,
            "content": combined[:8000],   # 8k chars per source for richer context
            "scraped_images": scraped_images[:3] # Keep top 3 images from this page
        }
    except Exception:
        # 🔄 Retry with a different user-agent (mobile UA bypasses some paywalls)
        try:
            async with httpx.AsyncClient() as retry_client:
                page_resp = await retry_client.get(
                    link, timeout=8.0, follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"}
                )
                soup = BeautifulSoup(page_resp.text, "html.parser")
                paragraphs = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 40]
                body_text = ' '.join(paragraphs)
                if body_text and len(body_text) > 200:
                    return {
                        "url": link, "title": title, "snippet": snippet,
                        "content": body_text[:8000], "scraped_images": []
                    }
        except Exception:
            pass
        
        # 📌 Last resort: use the Google snippet — real data, just shorter
        print(f"Scrape failed for {link}: using snippet ({len(snippet)} chars)")
        return {"url": link, "title": title, "snippet": snippet, "content": snippet}

async def _search_duckduckgo(query: str, num_results: int) -> list[dict]:
    """
    Fallback search using DuckDuckGo Instant Answer API (no API key needed).
    Returns limited but usable results when Google is unavailable.
    """
    print("⚠️ Google Search failed — falling back to DuckDuckGo.")
    results = []
    try:
        async with httpx.AsyncClient() as client:
            params = {"q": query, "format": "json", "no_redirect": "1", "no_html": "1"}
            resp = await client.get("https://api.duckduckgo.com/", params=params, timeout=5.0)
            data = resp.json()

            # DuckDuckGo Instant Answer
            abstract = data.get("Abstract", "")
            abstract_url = data.get("AbstractURL", "")
            abstract_source = data.get("AbstractSource", "Unknown")

            if abstract and abstract_url:
                results.append({
                    "url": abstract_url,
                    "title": abstract_source,
                    "snippet": abstract,
                    "content": abstract
                })

            # Related topics
            for topic in data.get("RelatedTopics", [])[:num_results - 1]:
                if isinstance(topic, dict) and topic.get("FirstURL"):
                    results.append({
                        "url": topic.get("FirstURL"),
                        "title": topic.get("Text", "")[:80],
                        "snippet": topic.get("Text", ""),
                        "content": topic.get("Text", "")
                    })
    except Exception as e:
        print(f"DuckDuckGo fallback also failed: {e}")

    return results

async def search_google(query: str, num_results: int = 10) -> list[dict]:
    """
    Primary: Google Custom Search with full page scraping.
    Fallback 1: DuckDuckGo Instant Answers (no API key required).
    Fallback 2: Hardcoded mock (dev mode).
    """
    if not GOOGLE_API_KEY or not SEARCH_ENGINE_ID:
        # Development mock fallback if API keys are not provided
        print("⚠️ No Google API key — using dev mock fallback.")
        return [{
            "url": "https://example.com/mock",
            "title": f"Mock result for {query}",
            "snippet": f"This is a mocked snippet about {query}.",
            "content": f"Full body text about {query}. This data would normally be scraped from the URL."
        }]

    url = "https://www.googleapis.com/customsearch/v1"
    
    results = []
    try:
        async with httpx.AsyncClient() as client:
            items = []
            pages_needed = (num_results + 9) // 10
            
            for page in range(pages_needed):
                start_idx = (page * 10) + 1
                fetch_num = min(10, num_results - (page * 10))
                
                params = {
                    "key": GOOGLE_API_KEY,
                    "cx": SEARCH_ENGINE_ID,
                    "q": query,
                    "num": fetch_num,
                    "start": start_idx
                }
                
                response = await client.get(url, params=params, timeout=8.0)
                if response.status_code != 200:
                    raise Exception(f"Google Search API returned {response.status_code}: {response.text[:200]}")

                data = response.json()
                page_items = data.get("items", [])
                
                if not page_items:
                    break
                
                items.extend(page_items)
                if len(items) >= num_results:
                    break

            if not items:
                raise Exception("Google returned 0 results.")

            # Scrape all pages concurrently for speed
            import asyncio
            scrape_tasks = [
                _scrape_url(client, item.get("link"), item.get("title"), item.get("snippet", ""))
                for item in items[:num_results] if item.get("link")
            ]
            results = await asyncio.gather(*scrape_tasks)

    except Exception as api_err:
        print(f"Google Search failed: {api_err}")
        # Fallback 1: DuckDuckGo
        results = await _search_duckduckgo(query, num_results)

    # Fallback 2: ensure we never return empty
    if not results:
        print("⚠️ All search providers failed — returning structured empty fallback.")
        results = [{
            "url": "https://fallback.local",
            "title": f"No live results for: {query}",
            "snippet": f"Search providers unavailable. Query: {query}",
            "content": f"Unable to retrieve live data for '{query}'. Please retry later."
        }]

    return results

async def fetch_images(query: str, num_results: int = 5) -> list[dict]:
    """
    Search for relevant images using Google Custom Search API.
    Returns: list of {"url": image_url, "title": image_title}
    """
    if not GOOGLE_API_KEY or not SEARCH_ENGINE_ID:
        return []

    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_API_KEY,
        "cx": SEARCH_ENGINE_ID,
        "q": query,
        "searchType": "image",
        "num": num_results,
        "safe": "active"
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=6.0)
            if response.status_code != 200:
                print(f"Image search API returned {response.status_code}")
                return []
            
            data = response.json()
            items = data.get("items", [])
            return [
                {
                    "url": item.get("link"),
                    "title": item.get("title", "")
                }
                for item in items if item.get("link")
            ]
    except Exception as e:
        print(f"Image search failed: {e}")
        return []

def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)

import xml.etree.ElementTree as ET

# async def search_wikipedia(query: str, num_results: int = 2) -> list[dict]:
#     """Search Wikipedia REST API for background knowledge."""
#     results = []
#     try:
#         async with httpx.AsyncClient() as client:
#             search_url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={query}&utf8=&format=json&srlimit={num_results}"
#             resp = await client.get(search_url, timeout=5.0)
#             data = resp.json()
#             search_items = data.get("query", {}).get("search", [])
#             for item in search_items:
#                 title = item["title"]
#                 page_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ', '_')}"
#                 page_resp = await client.get(page_url, timeout=5.0)
#                 if page_resp.status_code == 200:
#                     try:
#                         page_data = page_resp.json()
#                         results.append({
#                             "title": title,
#                             "url": page_data.get("content_urls", {}).get("desktop", {}).get("page", ""),
#                             "snippet": page_data.get("extract", "")
#                         })
#                     except Exception as e:
#                         print(f"Wikipedia JSON parse error for {title}: {e}")
#                     page_data = page_resp.json()
#                     extract = page_data.get("extract", "")
#                     content_url = page_data.get("content_urls", {}).get("desktop", {}).get("page", "")
#                     if extract and content_url:
#                         results.append({
#                             "url": content_url,
#                             "title": title + " (Wikipedia)",
#                             "snippet": item.get("snippet", ""),
#                             "content": extract
#                         })
#     except Exception as e:
#         print(f"Wikipedia search failed: {e}")
#     return results

# async def search_arxiv(query: str, num_results: int = 3) -> list[dict]:
#     """Search ArXiv API for scholarly papers."""
#     results = []
#     try:
#         async with httpx.AsyncClient() as client:
#             # Format query for Arxiv (replace spaces)
#             arxiv_query = query.replace(' ', '+')
#             arxiv_url = f"http://export.arxiv.org/api/query?search_query=all:{arxiv_query}&start=0&max_results={num_results}"
#             resp = await client.get(arxiv_url, timeout=8.0)
#             if resp.status_code == 200:
#                 root = ET.fromstring(resp.text)
#                 namespace = {'atom': 'http://www.w3.org/2005/Atom'}
#                 for entry in root.findall('atom:entry', namespace):
#                     title = entry.find('atom:title', namespace).text.replace('\\n', ' ')
#                     summary = entry.find('atom:summary', namespace).text.replace('\\n', ' ')
#                     link = entry.find('atom:id', namespace).text
#                     pdf_link = ""
#                     for link_elem in entry.findall('atom:link', namespace):
#                         if link_elem.get('title') == 'pdf':
#                             pdf_link = link_elem.get('href')
#                             break
#                     results.append({
#                         "url": pdf_link or link,
#                         "title": title + " (ArXiv)",
#                         "snippet": summary[:200] + "...",
#                         "content": summary
#                     })
#     except Exception as e:
#         print(f"ArXiv search failed: {e}")
#     return results

async def federated_search(query: str, num_results: int = 15) -> list[dict]:
    """Exclusively use Google Search for deeper crawling (Redesigned Phase)."""
    # Simply use Google for high-quality, real-time results
    # Wikipedia and ArXiv are removed as requested.
    return await search_google(query, num_results=num_results)
