#!/usr/bin/env python3
"""
Supplementary academic paper search: SSRN, NBER, HeinOnline, citation chaining,
and forthcoming paper lists.

Usage:
    python supplementary_search.py "corporate governance" -o results.json --all
    python supplementary_search.py "voting rules" --ssrn --nber -o results.json
    python supplementary_search.py "voting" --citation-chain --seeds-from prior.json

Requires: pip install requests beautifulsoup4 anthropic
API keys: SEARCHAPI_API_KEY (for SSRN, HeinOnline, forthcoming via Google Scholar)
           ANTHROPIC_API_KEY (for query condensation)
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Load ~/.lit-review-pipeline.env if present (portable key store), matching the
# other entry points so a standalone/agent-driven run finds SEARCHAPI/ANTHROPIC keys.
_env_file = Path.home() / ".lit-review-pipeline.env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file, override=False)
    except ImportError:
        pass

import requests
from bs4 import BeautifulSoup

# ── Constants ───────────────────────────────────────────────────────────────
SENTINEL = "SUPPLEMENTARY_DEFERRED"
REQUEST_TIMEOUT = 20
POLITE_DELAY = 1.0  # seconds between requests to same host
S2_DELAY = 3.5      # Semantic Scholar: ~100 req / 5 min = 1 per 3s
CACHE_TTL_HOURS = 24
SEARCHAPI_BASE = "https://www.searchapi.io/api/v1/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

S2_HEADERS = {
    "User-Agent": "LitReviewBot/1.0 (academic research tool)",
}


# ── Utility ─────────────────────────────────────────────────────────────────

def _defer(output_path, reason: str) -> None:
    """Graceful no-op: emit sentinel, write empty outputs, exit 0.

    Honors the pipeline's graceful-degradation contract so a missing key or an
    unexpected failure leaves an empty, valid output and a nonzero-free exit
    instead of crashing the run or silently looking complete-with-zero. Writes
    "[]" to the resolved JSON output and an empty ".ris" sibling, mirroring the
    file set the success path produces.
    """
    out = Path(output_path)
    json_out = out.parent / f"{out.stem}.json"
    ris_out = out.parent / f"{out.stem}.ris"
    print(f"{SENTINEL}: {reason}", flush=True)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text("[]", encoding="utf-8")
    ris_out.write_text("", encoding="utf-8")
    sys.exit(0)


def _norm_doi(doi: str) -> str:
    """Normalize DOI for deduplication."""
    if not doi:
        return ""
    doi = re.sub(r'^https?://doi\.org/', '', doi)
    return doi.lower().strip().rstrip("/.")


def _norm_title(title: str) -> str:
    """Normalize title for deduplication."""
    return re.sub(r'\s+', ' ', title.lower().strip()) if title else ""


def condense_query(raw_query: str) -> list[str]:
    """Use Claude Sonnet to generate 3-5 short Google Scholar queries."""
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": f"""Given the following research description, generate 3-5 short Google Scholar search queries (3-6 words each) that together cover the key literature streams. Return ONLY a JSON array of strings, no explanation.

Research description:
{raw_query}"""}],
    )
    text = resp.content[0].text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return json.loads(text)


def _dedup_papers(papers: list[dict]) -> list[dict]:
    """Deduplicate papers by DOI and title."""
    seen_dois: set[str] = set()
    seen_titles: set[str] = set()
    deduped = []

    for p in papers:
        doi = _norm_doi(p.get("doi", ""))
        title = _norm_title(p.get("title", ""))

        if doi and doi in seen_dois:
            continue
        if title and title in seen_titles:
            continue

        if doi:
            seen_dois.add(doi)
        if title:
            seen_titles.add(title)
        deduped.append(p)

    return deduped


def _load_seeds(path: str, top_n: int) -> list[str]:
    """Load seed DOIs from a prior pipeline JSON output file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    # Sort by score (descending), then citations, then take top N
    def sort_key(p):
        score = p.get("score", "")
        # Parse "95%" -> 95
        score_num = 0
        if score:
            m = re.search(r'(\d+)', str(score))
            if m:
                score_num = int(m.group(1))
        cites = int(p.get("citations", 0) or 0)
        return (score_num, cites)

    data.sort(key=sort_key, reverse=True)

    dois = []
    for p in data[:top_n * 2]:  # oversample to account for missing DOIs
        doi = p.get("doi", "")
        if doi:
            doi = re.sub(r'^https?://doi\.org/', '', doi).strip()
            if doi:
                dois.append(doi)
        if len(dois) >= top_n:
            break

    return dois


def _get_cache_path(source: str, query: str, debug_dir: Path) -> Path:
    """Get cache file path for a source+query combination."""
    key = hashlib.md5(f"{source}:{query}".encode()).hexdigest()[:12]
    return debug_dir / f"cache_{source}_{key}.json"


def _read_cache(cache_path: Path) -> list[dict] | None:
    """Read cached results if fresh enough."""
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        ts = data.get("timestamp", 0)
        if time.time() - ts < CACHE_TTL_HOURS * 3600:
            return data.get("papers", [])
    except Exception:
        pass
    return None


def _write_cache(cache_path: Path, papers: list[dict]) -> None:
    """Write results to cache."""
    cache_path.write_text(json.dumps({
        "timestamp": time.time(),
        "papers": papers,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


# ── SearchAPI (Google Scholar) helper ─────────────────────────────────────

def _search_google_scholar(query: str, max_results: int, source_tag: str,
                           journal_label: str, debug_dir: Path,
                           year_lo: int | None = None) -> list[dict]:
    """Search Google Scholar via SearchAPI.io.

    Args:
        query: Full search query (may include site: or source: operators).
        max_results: Maximum papers to return.
        source_tag: Value for the 'source' field (e.g. "ssrn", "forthcoming").
        journal_label: Value for the 'journal' field.
        debug_dir: Where to save raw API response for debugging.
        year_lo: Optional lower year bound (as_ylo param).
    """
    api_key = os.environ.get("SEARCHAPI_API_KEY", "")
    if not api_key:
        print("    SEARCHAPI_API_KEY not set — skipping Google Scholar search")
        return []

    papers = []
    # Google Scholar returns max 20 per page
    pages_needed = (min(max_results, 100) + 19) // 20

    for page_num in range(1, pages_needed + 1):
        params: dict = {
            "engine": "google_scholar",
            "q": query,
            "api_key": api_key,
            "num": "20",
            "page": str(page_num),
        }
        if year_lo is not None:
            params["as_ylo"] = str(year_lo)

        try:
            resp = requests.get(SEARCHAPI_BASE, params=params, timeout=30)
            if resp.status_code != 200:
                print(f"    SearchAPI HTTP {resp.status_code}")
                break

            data = resp.json()

            # Save debug response (first page only)
            if page_num == 1:
                slug = re.sub(r'[^a-z0-9]+', '_', source_tag.lower())
                (debug_dir / f"searchapi_{slug}.json").write_text(
                    json.dumps(data, indent=2, ensure_ascii=False)[:50000],
                    encoding="utf-8",
                )

            results = data.get("organic_results", [])
            if not results:
                break

            for item in results:
                title = item.get("title", "")
                if not title or len(title) < 10:
                    continue

                # Authors from the authors array
                authors_list = item.get("authors", [])
                if authors_list:
                    authors = ", ".join(
                        a.get("name", "") for a in authors_list
                        if a.get("name")
                    )
                else:
                    # Fall back to publication_info string
                    pub = item.get("publication", "") or ""
                    authors = pub.split(" - ")[0].strip() if " - " in pub else ""

                # Year from publication string
                year = ""
                pub_info = item.get("publication", "") or ""
                year_match = re.search(r'\b(19|20)\d{2}\b', pub_info)
                if year_match:
                    year = year_match.group(0)

                link = item.get("link", "")
                snippet = item.get("snippet", "")

                papers.append({
                    "title": title,
                    "authors": authors,
                    "year": year,
                    "doi": "",
                    "abstract": snippet,
                    "journal": journal_label,
                    "url": link,
                    "source": source_tag,
                })

            if len(papers) >= max_results:
                break

            time.sleep(POLITE_DELAY)

        except Exception as e:
            print(f"    SearchAPI error: {e}")
            break

    return _dedup_papers(papers)[:max_results]


# ── SSRN ────────────────────────────────────────────────────────────────────

def search_google_scholar_plain(queries: list[str], max_results: int,
                                debug_dir: Path) -> list[dict]:
    """Search Google Scholar directly (no site/source filter) for each query."""
    print(f"  [SCHOLAR] Searching Google Scholar ({len(queries)} queries)...")
    per_q = min(20, max_results) if max_results else 20
    all_papers: list[dict] = []
    for i, q in enumerate(queries):
        papers = _search_google_scholar(
            str(q), per_q, "google_scholar", "", debug_dir,
        )
        all_papers.extend(papers)
        print(f"    [q{i}] '{q}' -> {len(papers)} results")
        if i < len(queries) - 1:
            time.sleep(POLITE_DELAY)
    result = _dedup_papers(all_papers)
    print(f"  [SCHOLAR] Found {len(result)} unique papers")
    return result


def search_ssrn(queries: list[str], max_results: int, debug_dir: Path) -> list[dict]:
    """Search SSRN via Google Scholar (SearchAPI) with site:ssrn.com filter."""
    print(f"  [SSRN] Searching via Google Scholar ({len(queries)} queries)...")
    all_papers = []
    for i, q in enumerate(queries):
        gs_query = f"{q} site:ssrn.com"
        papers = _search_google_scholar(
            gs_query, max_results, "ssrn", "SSRN Working Paper", debug_dir,
        )
        all_papers.extend(papers)
        if i < len(queries) - 1:
            time.sleep(POLITE_DELAY)
    result = _dedup_papers(all_papers)[:max_results]
    print(f"  [SSRN] Found {len(result)} papers")
    return result


# ── HeinOnline ──────────────────────────────────────────────────────────────

def search_heinonline(queries: list[str], max_results: int, debug_dir: Path) -> list[dict]:
    """Search HeinOnline via Google Scholar (SearchAPI) with site:heinonline.org filter."""
    print(f"  [HEINONLINE] Searching via Google Scholar ({len(queries)} queries)...")
    all_papers = []
    for i, q in enumerate(queries):
        gs_query = f"{q} site:heinonline.org"
        papers = _search_google_scholar(
            gs_query, max_results, "heinonline", "HeinOnline", debug_dir,
        )
        all_papers.extend(papers)
        if i < len(queries) - 1:
            time.sleep(POLITE_DELAY)
    result = _dedup_papers(all_papers)[:max_results]
    print(f"  [HEINONLINE] Found {len(result)} papers")
    return result


# ── NBER ────────────────────────────────────────────────────────────────────

def search_nber(queries: list[str], max_results: int, debug_dir: Path) -> list[dict]:
    """Search NBER working paper series (one API call per condensed query)."""
    print(f"  [NBER] Searching ({len(queries)} queries)...")
    all_papers = []

    for qi, query in enumerate(queries):
        url = "https://www.nber.org/api/v1/working_page_listing/contentType/working_paper/_/_/search"
        params = {
            "page": "1",
            "perPage": str(min(max_results, 100)),
            "q": query,
        }

        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)

            if resp.status_code != 200:
                print(f"    API returned {resp.status_code}, trying HTML search...")
                all_papers.extend(_search_nber_html(query, max_results, debug_dir))
                if qi < len(queries) - 1:
                    time.sleep(POLITE_DELAY)
                continue

            data = resp.json()
            results = data.get("results", [])

            for item in results[:max_results]:
                title = item.get("title", "")
                if not title:
                    continue

                # Authors — API returns HTML like '<a href="/people/x">Name</a>'
                authors_list = item.get("authors", [])
                if isinstance(authors_list, list):
                    author_names = []
                    for a in authors_list:
                        raw = a.get("name", "") if isinstance(a, dict) else str(a)
                        clean = re.sub(r'<[^>]+>', '', raw).strip()
                        if clean:
                            author_names.append(clean)
                    authors = ", ".join(author_names)
                else:
                    authors = re.sub(r'<[^>]+>', '', str(authors_list)).strip()

                year = str(item.get("year", ""))
                if not year:
                    dd = item.get("displaydate", "")
                    if dd:
                        ym = re.search(r'\b(19|20)\d{2}\b', dd)
                        if ym:
                            year = ym.group(0)

                doi = item.get("doi", "")
                url_val = item.get("url", "")
                if url_val and not url_val.startswith("http"):
                    url_val = f"https://www.nber.org{url_val}"

                all_papers.append({
                    "title": title,
                    "authors": authors,
                    "year": year,
                    "doi": doi,
                    "abstract": item.get("abstract", ""),
                    "journal": "NBER Working Paper",
                    "url": url_val,
                    "source": "nber",
                })

        except Exception as e:
            print(f"    Error: {e}")
            all_papers.extend(_search_nber_html(query, max_results, debug_dir))

        if qi < len(queries) - 1:
            time.sleep(POLITE_DELAY)

    result = _dedup_papers(all_papers)[:max_results]
    print(f"  [NBER] Found {len(result)} papers")
    return result


def _search_nber_html(query: str, max_results: int, debug_dir: Path) -> list[dict]:
    """Fallback: scrape NBER search results from HTML."""
    papers = []
    url = f"https://www.nber.org/search?q={urllib.parse.quote(query)}&type=working_paper"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        (debug_dir / "nber_search.html").write_text(resp.text, encoding="utf-8")

        soup = BeautifulSoup(resp.text, "html.parser")

        # Look for paper links
        for link in soup.select('a[href*="/papers/w"]')[:max_results]:
            title = link.get_text(strip=True)
            href = link.get("href", "")
            if not title or len(title) < 10:
                continue

            if not href.startswith("http"):
                href = f"https://www.nber.org{href}"

            papers.append({
                "title": title,
                "authors": "",
                "year": "",
                "doi": "",
                "abstract": "",
                "journal": "NBER Working Paper",
                "url": href,
                "source": "nber",
            })

    except Exception as e:
        print(f"    HTML fallback error: {e}")

    return papers


# ── Citation Chaining (Semantic Scholar) ────────────────────────────────────

def citation_chain(seed_dois: list[str], query: str, hops: int,
                   max_results: int, debug_dir: Path) -> list[dict]:
    """Forward and backward citation traversal via Semantic Scholar API."""
    print(f"  [CITATION] Chaining from {len(seed_dois)} seeds, {hops} hop(s)...")
    papers = []
    seen_ids: set[str] = set()
    current_ids: list[str] = []

    # Resolve seed DOIs to Semantic Scholar paper IDs
    for doi in seed_dois:
        paper = _s2_get_paper(doi)
        if paper:
            pid = paper.get("paperId", "")
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                current_ids.append(pid)
        time.sleep(S2_DELAY)

    print(f"    Resolved {len(current_ids)}/{len(seed_dois)} seeds")

    # Traverse hops
    for hop in range(hops):
        next_ids: list[str] = []
        print(f"    Hop {hop + 1}: processing {len(current_ids)} papers...")

        for pid in current_ids:
            if len(papers) >= max_results:
                break

            # Forward citations (papers that cite this one)
            fwd = _s2_get_citations(pid) or []
            for item in fwd:
                cited_paper = item.get("citingPaper", {})
                cpid = cited_paper.get("paperId", "")
                if cpid and cpid not in seen_ids:
                    seen_ids.add(cpid)
                    next_ids.append(cpid)
                    p = _s2_paper_to_dict(cited_paper)
                    if p:
                        papers.append(p)

            time.sleep(S2_DELAY)

            # Backward references (papers this one cites)
            bwd = _s2_get_references(pid) or []
            for item in bwd:
                ref_paper = item.get("citedPaper", {})
                rpid = ref_paper.get("paperId", "")
                if rpid and rpid not in seen_ids:
                    seen_ids.add(rpid)
                    next_ids.append(rpid)
                    p = _s2_paper_to_dict(ref_paper)
                    if p:
                        papers.append(p)

            time.sleep(S2_DELAY)

        current_ids = next_ids[:max_results]  # limit fan-out

    # Filter by query relevance if we have too many
    if query and len(papers) > max_results:
        papers = _filter_by_relevance(papers, query, max_results)

    papers = _dedup_papers(papers)[:max_results]
    print(f"  [CITATION] Found {len(papers)} papers via citation chaining")
    return papers


def _s2_get_paper(doi: str) -> dict | None:
    """Get paper metadata from Semantic Scholar by DOI."""
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
    params = {"fields": "paperId,title,authors,year,externalIds,abstract,venue,citationCount"}
    try:
        resp = requests.get(url, params=params, headers=S2_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _s2_get_citations(paper_id: str, limit: int = 50) -> list[dict]:
    """Get forward citations (papers that cite this one)."""
    url = f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}/citations"
    params = {
        "fields": "paperId,title,authors,year,externalIds,venue,citationCount,abstract",
        "limit": str(limit),
    }
    try:
        resp = requests.get(url, params=params, headers=S2_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return resp.json().get("data", [])
    except Exception:
        pass
    return []


def _s2_get_references(paper_id: str, limit: int = 50) -> list[dict]:
    """Get backward references (papers this one cites)."""
    url = f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}/references"
    params = {
        "fields": "paperId,title,authors,year,externalIds,venue,citationCount,abstract",
        "limit": str(limit),
    }
    try:
        resp = requests.get(url, params=params, headers=S2_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return resp.json().get("data", [])
    except Exception:
        pass
    return []


def _s2_paper_to_dict(paper: dict) -> dict | None:
    """Convert Semantic Scholar paper to our standard dict."""
    title = paper.get("title")
    if not title:
        return None

    authors = ", ".join(
        a.get("name", "") for a in (paper.get("authors") or [])
        if a.get("name")
    )

    ext_ids = paper.get("externalIds") or {}
    doi = ext_ids.get("DOI", "")
    if doi:
        doi = f"https://doi.org/{doi}"

    return {
        "title": title,
        "authors": authors,
        "year": str(paper.get("year") or ""),
        "doi": doi,
        "abstract": paper.get("abstract") or "",
        "journal": paper.get("venue") or "",
        "url": doi or "",
        "source": "citation_chain",
    }


def _filter_by_relevance(papers: list[dict], query: str, limit: int) -> list[dict]:
    """Simple keyword relevance filter."""
    query_words = set(w.lower() for w in query.split() if len(w) > 2)

    def score(p):
        text = f"{p.get('title', '')} {p.get('abstract', '')}".lower()
        return sum(1 for w in query_words if w in text)

    papers.sort(key=score, reverse=True)
    return papers[:limit]


# ── Forthcoming Papers ──────────────────────────────────────────────────────

def search_forthcoming(queries: list[str], max_results: int, use_cache: bool,
                       debug_dir: Path) -> list[dict]:
    """Search recent papers from top finance journals via Google Scholar."""
    print(f"  [FORTHCOMING] Searching JF, JFE, RFS via Google Scholar ({len(queries)} queries)...")
    all_papers = []

    # Google Scholar source: filter for each journal
    journals = [
        ("JF", "The Journal of Finance"),
        ("JFE", "Journal of Financial Economics"),
        ("RFS", "Review of Financial Studies"),
    ]

    import datetime
    current_year = datetime.date.today().year

    for abbrev, display_name in journals:
        for qi, q in enumerate(queries):
            cache_key = f"forthcoming_{abbrev}_{qi}"
            cache_path = _get_cache_path(cache_key, q, debug_dir)

            if use_cache:
                cached = _read_cache(cache_path)
                if cached is not None:
                    print(f"    [{abbrev}][q{qi}] Using cached results ({len(cached)} papers)")
                    all_papers.extend(cached)
                    continue

            try:
                gs_query = f'{q} source:"{display_name}"'
                journal_papers = _search_google_scholar(
                    gs_query,
                    max_results=20,  # per journal per query
                    source_tag="forthcoming",
                    journal_label=f"{display_name} (Recent)",
                    debug_dir=debug_dir,
                    year_lo=current_year - 1,
                )
                if use_cache and journal_papers:
                    _write_cache(cache_path, journal_papers)
                all_papers.extend(journal_papers)
                print(f"    [{abbrev}][q{qi}] Found {len(journal_papers)} recent papers")
            except Exception as e:
                print(f"    [{abbrev}][q{qi}] Error: {e}")

            time.sleep(POLITE_DELAY)

    all_papers = _dedup_papers(all_papers)[:max_results]
    print(f"  [FORTHCOMING] {len(all_papers)} papers total")
    return all_papers


# ── Output ──────────────────────────────────────────────────────────────────

def normalize_results(papers: list[dict]) -> list[dict]:
    """Normalize into consistent schema."""
    normalized = []
    for p in papers:
        normalized.append({
            "title": p.get("title", ""),
            "authors": p.get("authors", ""),
            "year": p.get("year", ""),
            "doi": p.get("doi", ""),
            "abstract": p.get("abstract", ""),
            "journal": p.get("journal", ""),
            "relevance": "",
            "score": "",
            "url": p.get("url") or p.get("doi", ""),
            "source": p.get("source", "supplementary"),
        })
    return normalized


def write_ris(papers: list[dict], path: Path) -> None:
    """Write papers in RIS format."""
    lines = []
    for p in papers:
        lines.append("TY  - JOUR")
        if p.get("title"):
            lines.append(f"T1  - {p['title']}")
        for author in (p.get("authors") or "").split(", "):
            author = author.strip()
            if author:
                lines.append(f"AU  - {author}")
        if p.get("journal"):
            lines.append(f"JO  - {p['journal']}")
        if p.get("year"):
            lines.append(f"PY  - {p['year']}")
        if p.get("doi"):
            doi = re.sub(r'^https?://doi\.org/', '', p["doi"])
            lines.append(f"DO  - {doi}")
        if p.get("abstract"):
            lines.append(f"AB  - {p['abstract']}")
        if p.get("url"):
            lines.append(f"UR  - {p['url']}")
        lines.append("ER  -")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Supplementary academic paper search (SSRN, NBER, ECGI, citations, forthcoming)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("query", nargs="?", default="",
                        help="Research query (optional if --queries-file is given)")
    parser.add_argument(
        "-o", "--output", default="supplementary_results.json",
        help="Output JSON path (default: supplementary_results.json)",
    )
    # Source flags
    parser.add_argument("--ssrn", action="store_true", help="Search SSRN")
    parser.add_argument("--nber", action="store_true", help="Search NBER")
    parser.add_argument("--citation-chain", action="store_true",
                        help="Citation traversal via Semantic Scholar")
    parser.add_argument("--forthcoming", action="store_true",
                        help="Forthcoming paper lists from JF, JFE, RFS")
    parser.add_argument("--heinonline", action="store_true",
                        help="Search HeinOnline via Google Scholar")
    parser.add_argument("--scholar", action="store_true",
                        help="Search Google Scholar directly (no site filter)")
    parser.add_argument("--all", action="store_true",
                        help="Enable all sources")
    # Citation chain options
    parser.add_argument("--seeds-from", default="",
                        help="JSON file to extract seed DOIs from")
    parser.add_argument("--top-seeds", type=int, default=10,
                        help="Number of top papers to use as seeds (default: 10)")
    parser.add_argument("--hops", type=int, default=1,
                        help="Citation chain depth (default: 1)")
    # Other options
    parser.add_argument("--cache", action="store_true",
                        help="Cache forthcoming lists")
    parser.add_argument("--max-per-source", type=int, default=100,
                        help="Max papers per source (default: 100)")
    parser.add_argument("--debug-dir", default="supplementary_debug",
                        help="Directory for debug output (default: supplementary_debug)")
    parser.add_argument("--no-condense", action="store_true",
                        help="Skip query condensation; use raw query as-is")
    parser.add_argument("--queries-file", default="",
                        help="JSON file with an array of query strings; overrides "
                             "condensation and the positional query for keyword sources")

    args = parser.parse_args()

    # Resolve --all
    if args.all:
        args.ssrn = args.nber = args.citation_chain = args.forthcoming = True
        args.heinonline = args.scholar = True

    # Check at least one source is enabled
    if not any([args.ssrn, args.nber, args.citation_chain, args.forthcoming,
                args.heinonline, args.scholar]):
        parser.error("No sources enabled. Use --scholar, --ssrn, --nber, "
                      "--citation-chain, --forthcoming, --heinonline, or --all")

    # Citation chain needs seeds
    if args.citation_chain and not args.seeds_from:
        parser.error("--citation-chain requires --seeds-from <json-file>")

    # Keyword sources need either a query or a queries file
    keyword_sources = any([args.ssrn, args.nber, args.forthcoming,
                           args.heinonline, args.scholar])
    if keyword_sources and not args.query and not args.queries_file:
        parser.error("Keyword sources need a query or --queries-file")

    debug_dir = Path(args.debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)

    output_path = Path(args.output)
    output_dir = output_path.parent
    output_stem = output_path.stem
    json_path = output_dir / f"{output_stem}.json"
    ris_path = output_dir / f"{output_stem}.ris"

    print("=== Supplementary Search ===")
    print(f"Query: {args.query}")
    print(f"Output: {json_path} + {ris_path}")
    sources = []
    if args.scholar: sources.append("Google Scholar")
    if args.ssrn: sources.append("SSRN")
    if args.nber: sources.append("NBER")
    if args.heinonline: sources.append("HeinOnline")
    if args.citation_chain: sources.append("Citation Chain")
    if args.forthcoming: sources.append("Forthcoming")
    print(f"Sources: {', '.join(sources)}")
    print()

    all_papers = []

    try:
        # SearchAPI-backed sources (Google Scholar and its site:/source: variants)
        # all require SEARCHAPI_API_KEY. If any such source is enabled but the key
        # is missing, defer (sentinel + empty output + exit 0) rather than silently
        # returning [] or crashing. NBER (own API) and citation chaining (Semantic
        # Scholar) do not need the key, so only defer when no usable source remains.
        searchapi_sources = any([args.scholar, args.ssrn, args.heinonline,
                                 args.forthcoming])
        non_searchapi_sources = any([args.nber, args.citation_chain])
        if (searchapi_sources and not non_searchapi_sources
                and not os.environ.get("SEARCHAPI_API_KEY", "")):
            _defer(output_path,
                   "SEARCHAPI_API_KEY not set; no SearchAPI-independent source enabled.")

        # Query set for keyword sources: explicit file > raw query (no condense) >
        # Claude condensation. Citation chaining runs directly from seed DOIs, so it
        # must never trigger condensation — doing so would call Claude on an empty
        # query and demand ANTHROPIC_API_KEY even on the agent-driven, no-API path.
        condensed: list = []
        if args.queries_file:
            condensed = json.loads(Path(args.queries_file).read_text(encoding="utf-8"))
            if not args.query:
                args.query = " ".join(str(q) for q in condensed[:5])
            print(f"Using {len(condensed)} queries from {args.queries_file}")
        elif keyword_sources and args.no_condense:
            condensed = [args.query]
            print("Condensation skipped (--no-condense). Using raw query.")
        elif keyword_sources:
            print("Condensing query via Claude Sonnet...")
            condensed = condense_query(args.query)
            print(f"Condensed into {len(condensed)} queries: {condensed}")
        print()

        if args.scholar:
            papers = search_google_scholar_plain(condensed, args.max_per_source, debug_dir)
            all_papers.extend(papers)

        if args.ssrn:
            papers = search_ssrn(condensed, args.max_per_source, debug_dir)
            all_papers.extend(papers)

        if args.nber:
            papers = search_nber(condensed, args.max_per_source, debug_dir)
            all_papers.extend(papers)

        if args.heinonline:
            papers = search_heinonline(condensed, args.max_per_source, debug_dir)
            all_papers.extend(papers)

        if args.citation_chain:
            seed_dois = _load_seeds(args.seeds_from, args.top_seeds)
            print(f"  Loaded {len(seed_dois)} seed DOIs from {args.seeds_from}")
            papers = citation_chain(
                seed_dois, args.query or "", args.hops,
                args.max_per_source, debug_dir,
            )
            all_papers.extend(papers)

        if args.forthcoming:
            papers = search_forthcoming(
                condensed, args.max_per_source, args.cache, debug_dir,
            )
            all_papers.extend(papers)

        # Deduplicate across all sources
        all_papers = _dedup_papers(all_papers)

        # Normalize
        results = normalize_results(all_papers)

        # Write JSON
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(results, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nSaved {len(results)} papers to {json_path}")

        # Write RIS
        write_ris(results, ris_path)
        print(f"RIS saved to {ris_path}")

        # Summary by source
        source_counts: dict[str, int] = {}
        for p in results:
            s = p.get("source", "unknown")
            source_counts[s] = source_counts.get(s, 0) + 1
        print("\nBy source:")
        for s, c in sorted(source_counts.items()):
            print(f"  {s}: {c}")

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(130)
    except SystemExit:
        # Intentional defer/exit (e.g. _defer) — never swallow it.
        raise
    except Exception as e:
        # Any unexpected failure defers instead of crashing the pipeline:
        # sentinel + empty output for the resolved -o path + exit 0.
        print(f"\nError: {e}")
        print(f"Debug output saved to: {debug_dir}")
        _defer(output_path,
               f"Unexpected failure ({type(e).__name__}: {str(e)[:160]}).")


if __name__ == "__main__":
    main()
