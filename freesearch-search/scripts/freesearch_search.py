#!/usr/bin/env python3
"""Free index search (Stage 4e) — keyless lexical search of OpenAlex/Crossref/S2.

A zero-key discovery channel: it runs the Stage-0 query list against the FREE,
keyless keyword-search endpoints of OpenAlex, Crossref, and Semantic Scholar,
normalizes the real records into the pipeline schema, dedups across the three
indexes, and writes the stage JSON + .ris. It pairs with the agent web-search
channel as the "only Claude Code" search fallback: every hit is a real index
record (so fabrication is essentially nil), and Stage 5b verification still
confirms each one downstream.

Needs NO API key. OpenAlex and Crossref use the polite pool via a mailto; Semantic
Scholar's keyless tier works but is rate-limited. If you happen to have keys, they
are used to raise limits: OPENALEX_API_KEY and/or SEMANTIC_SCHOLAR_API_KEY. It
makes no LLM API calls.

Usage:
  python freesearch_search.py --queries-file OUT/scholar_queries.json -o OUT/stage4e_freesearch.json
  python freesearch_search.py --query "dual-class shares cost of equity" -o out.json
  python freesearch_search.py --queries-file q.json -o out.json --per-query 15 --sources openalex,crossref
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

# Load ~/.lit-review-pipeline.env if present (portable key store / contact email).
_env_file = Path.home() / ".lit-review-pipeline.env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file, override=False)
    except ImportError:
        pass

import requests

SENTINEL = "FREESEARCH_DEFERRED"
_CONTACT = os.environ.get("LITREVIEW_CONTACT_EMAIL", "litreview-bot@example.com")
OA_BASE = "https://api.openalex.org/works"
CR_BASE = "https://api.crossref.org/works"
S2_BASE = "https://api.semanticscholar.org/graph/v1/paper/search"
REQUEST_TIMEOUT = 25


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").lower().strip())


def _norm_doi(s: str) -> str:
    m = re.search(r"10\.\d{4,9}/\S+", (s or "").strip(), re.I)
    return f"https://doi.org/{m.group(0).rstrip(').,;').lower()}" if m else ""


def _reconstruct_abstract(inv: dict | None) -> str:
    """Rebuild abstract text from OpenAlex's inverted-index format."""
    if not inv or not isinstance(inv, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)


def _schema(title, authors, year, doi, abstract, journal, url, source, citations=0,
            open_access=False) -> dict:
    return {
        "title": (title or "").strip(),
        "authors": (authors or "").strip(),
        "year": str(year or "").strip(),
        "doi": _norm_doi(doi) or "",
        "abstract": (abstract or "").strip(),
        "journal": (journal or "").strip(),
        "url": (url or "").strip(),
        "source": source,
        "verified": False,
        "citations": int(citations or 0),
        "open_access": bool(open_access),
    }


def search_openalex(query: str, per_query: int) -> list[dict]:
    params = {
        "search": query,
        "per_page": str(min(per_query, 200)),
        "select": "title,publication_year,authorships,doi,primary_location,"
                  "abstract_inverted_index,cited_by_count,open_access",
    }
    key = os.environ.get("OPENALEX_API_KEY", "")
    if key:
        params["api_key"] = key
    else:
        params["mailto"] = _CONTACT
    out: list[dict] = []
    r = requests.get(OA_BASE, params=params, timeout=REQUEST_TIMEOUT,
                     headers={"User-Agent": f"LitReviewPipeline/1.0 (mailto:{_CONTACT})"})
    if not r.ok:
        return out
    for w in (r.json().get("results") or []):
        authors = ", ".join(
            (a.get("author", {}) or {}).get("display_name", "").strip()
            for a in (w.get("authorships") or []) if (a.get("author") or {}).get("display_name"))
        loc = (w.get("primary_location") or {})
        journal = ((loc.get("source") or {}) or {}).get("display_name", "") or ""
        url = loc.get("landing_page_url") or w.get("doi") or ""
        out.append(_schema(
            w.get("title", ""), authors, w.get("publication_year", ""), w.get("doi", ""),
            _reconstruct_abstract(w.get("abstract_inverted_index")), journal, url,
            "openalex", w.get("cited_by_count", 0),
            ((w.get("open_access") or {}) or {}).get("is_oa", False)))
    return out


def search_crossref(query: str, per_query: int) -> list[dict]:
    params = {"query.bibliographic": query, "rows": str(min(per_query, 100)), "mailto": _CONTACT}
    out: list[dict] = []
    r = requests.get(CR_BASE, params=params, timeout=REQUEST_TIMEOUT,
                     headers={"User-Agent": f"LitReviewPipeline/1.0 (mailto:{_CONTACT})"})
    if not r.ok:
        return out
    for it in (r.json().get("message", {}).get("items") or []):
        titles = it.get("title") or []
        if not titles:
            continue
        authors = ", ".join(
            f"{a.get('given', '').strip()} {a.get('family', '').strip()}".strip()
            for a in (it.get("author") or []) if (a.get("family") or a.get("given")))
        dp = (it.get("issued", {}).get("date-parts") or [[None]])[0]
        year = dp[0] if dp else ""
        journal = " ".join(it.get("container-title") or [])
        doi = it.get("DOI", "")
        out.append(_schema(titles[0], authors, year, doi, "", journal,
                           it.get("URL", "") or (f"https://doi.org/{doi}" if doi else ""),
                           "crossref", it.get("is-referenced-by-count", 0)))
    return out


def search_semanticscholar(query: str, per_query: int) -> list[dict]:
    params = {"query": query, "limit": str(min(per_query, 100)),
              "fields": "title,year,authors,abstract,venue,externalIds,citationCount,url,openAccessPdf"}
    headers = {"User-Agent": f"LitReviewPipeline/1.0 (mailto:{_CONTACT})"}
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "") or os.environ.get("S2_API_KEY", "")
    if key:
        headers["x-api-key"] = key
    out: list[dict] = []
    r = requests.get(S2_BASE, params=params, timeout=REQUEST_TIMEOUT, headers=headers)
    if not r.ok:
        return out
    for p in (r.json().get("data") or []):
        authors = ", ".join((a.get("name", "") or "").strip()
                            for a in (p.get("authors") or []) if a.get("name"))
        doi = (p.get("externalIds") or {}).get("DOI", "")
        out.append(_schema(p.get("title", ""), authors, p.get("year", ""), doi,
                           p.get("abstract", ""), p.get("venue", ""),
                           p.get("url", "") or (f"https://doi.org/{doi}" if doi else ""),
                           "semanticscholar", p.get("citationCount", 0),
                           bool(p.get("openAccessPdf"))))
    return out


_FETCHERS = {
    "openalex": (search_openalex, 0.2),
    "crossref": (search_crossref, 0.2),
    "semanticscholar": (search_semanticscholar, 1.2),  # keyless S2 is rate-limited
}


def _dedup(records: list[dict]) -> list[dict]:
    """Merge across indexes: key on DOI when present, else normalized title."""
    seen: dict[str, dict] = {}
    out: list[dict] = []
    for r in records:
        key = r.get("doi") or _norm_title(r.get("title", ""))
        if not key:
            continue
        if key in seen:
            # Fill gaps on the kept record from the duplicate (e.g. abstract, doi).
            tgt = seen[key]
            for f in ("authors", "year", "doi", "abstract", "journal", "url"):
                if not tgt.get(f) and r.get(f):
                    tgt[f] = r[f]
            if r.get("citations", 0) > tgt.get("citations", 0):
                tgt["citations"] = r["citations"]
        else:
            seen[key] = r
            out.append(r)
    return out


def write_ris(records: list[dict], path: Path) -> None:
    lines: list[str] = []
    for p in records:
        lines.append("TY  - JOUR")
        for au in (p.get("authors") or "").split(","):
            au = au.strip()
            if au:
                lines.append(f"AU  - {au}")
        if p.get("title"):
            lines.append(f"TI  - {p['title']}")
        if p.get("journal"):
            lines.append(f"JO  - {p['journal']}")
        if p.get("year"):
            lines.append(f"PY  - {p['year']}")
        doi = (p.get("doi") or "").replace("https://doi.org/", "")
        if doi:
            lines.append(f"DO  - {doi}")
        if p.get("abstract"):
            lines.append(f"AB  - {p['abstract']}")
        if p.get("url"):
            lines.append(f"UR  - {p['url']}")
        lines.append("ER  - ")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def run(queries: list[str], sources: list[str], per_query: int, output: Path) -> list[dict]:
    all_records: list[dict] = []
    for q in queries:
        q = (q or "").strip()
        if not q:
            continue
        for src in sources:
            fetch, delay = _FETCHERS[src]
            try:
                hits = fetch(q, per_query)
                all_records.extend(hits)
                print(f"  [{src}] '{q[:60]}' -> {len(hits)} results")
            except Exception as e:
                print(f"  [{src}] '{q[:60]}' -> error ({type(e).__name__}: {str(e)[:80]})")
            time.sleep(delay)
    merged = _dedup(all_records)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    write_ris(merged, output.with_suffix(".ris"))
    return merged


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Keyless lexical search of OpenAlex/Crossref/Semantic Scholar (Stage 4e).")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--queries-file", help="JSON array of query strings (e.g. scholar_queries.json)")
    grp.add_argument("--query", help="A single query string")
    ap.add_argument("-o", "--output", default="stage4e_freesearch.json", help="Output JSON path")
    ap.add_argument("--per-query", type=int, default=20, help="Results per query per source (default 20)")
    ap.add_argument("--sources", default="openalex,crossref,semanticscholar",
                    help="Comma-separated subset of: openalex,crossref,semanticscholar")
    args = ap.parse_args()

    output = Path(args.output)
    sources = [s.strip().lower() for s in args.sources.split(",") if s.strip() in _FETCHERS]
    if not sources:
        print(f"{SENTINEL}: no valid --sources; nothing to search.")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("[]", encoding="utf-8")
        sys.exit(0)

    if args.queries_file:
        try:
            queries = json.loads(Path(args.queries_file).read_text(encoding="utf-8"))
            queries = [q for q in queries if isinstance(q, str)]
        except Exception as e:
            print(f"{SENTINEL}: could not read --queries-file ({e}); writing empty output.")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("[]", encoding="utf-8")
            sys.exit(0)
    else:
        queries = [args.query]

    if not queries:
        print(f"{SENTINEL}: no queries to run; writing empty output.")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("[]", encoding="utf-8")
        sys.exit(0)

    print(f"Free index search: {len(queries)} queries x {sources} (keyless)")
    res = run(queries, sources, args.per_query, output)
    print(f"[FREESEARCH] {len(res)} unique papers -> {output}")


if __name__ == "__main__":
    main()
