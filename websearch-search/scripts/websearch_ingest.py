#!/usr/bin/env python3
"""Web-search ingest (Stage 4d) — normalize agent-gathered web hits into records.

The web-search channel has NO subprocess driver. In the agent-driven (Claude Code)
flow the agent itself runs its WebSearch / WebFetch tools over the Stage-0 queries
and the manuscript's topics, gathers candidate papers, and writes them as a simple
JSON array. This script validates and normalizes those candidates into the pipeline
schema (source="websearch"), dedups by title, optionally fills missing DOIs from
Crossref (keyless, best-effort), and writes the stage JSON + .ris sibling so the
results flow into dedup -> verify -> screen like any other channel.

Web hits are REAL search results, not model memory, so fabrication is low -- but
Stage 5b verification still confirms every paper, so keep verification ON at dedup.

This channel needs NO external account or API key: the agent's web tools do the
search, and only a keyless Crossref polite-pool call is used for optional DOI fill.
It makes no Anthropic/DeepSeek/LLM API calls.

Input JSON (what the agent writes) — an array of objects; only `title` is required:
  [{"title": "...", "authors": "First Last, First Last", "year": "2021",
    "journal": "...", "doi": "10.xxxx/...", "url": "https://...",
    "abstract": "..."}, ...]
A bare {"papers": [...]} / {"results": [...]} / {"candidates": [...]} wrapper is
also accepted.

Usage:
  python websearch_ingest.py --results OUT/websearch_results.json -o OUT/stage4d_websearch.json
  python websearch_ingest.py --results OUT/websearch_results.json -o OUT/stage4d_websearch.json --no-enrich
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
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

# Polite-pool contact for Crossref (courtesy only); override via env, neutral default.
_CONTACT = os.environ.get("LITREVIEW_CONTACT_EMAIL", "litreview-bot@example.com")
DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.I)


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").lower().strip())


def _title_sim(a: str, b: str) -> float:
    wa, wb = set(_norm_title(a).split()), set(_norm_title(b).split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _norm_doi(s: str) -> str:
    """Extract a normalized DOI URL from a doi/url field, else ''."""
    m = DOI_RE.search((s or "").strip())
    return f"https://doi.org/{m.group(0).rstrip(').,;')}" if m else ""


def _coerce(rec: dict) -> dict | None:
    """Map a raw agent candidate to the pipeline schema; drop it if no usable title."""
    if not isinstance(rec, dict):
        return None
    title = re.sub(r"\s+", " ", str(rec.get("title", "") or "").strip())
    if len(title) < 6:
        return None
    year = ""
    ym = re.search(r"(19|20)\d{2}", str(rec.get("year", "") or ""))
    if ym:
        year = ym.group(0)
    doi = _norm_doi(rec.get("doi", "")) or _norm_doi(rec.get("url", ""))
    return {
        "title": title,
        "authors": str(rec.get("authors", "") or "").strip(),
        "year": year,
        "doi": doi,
        "abstract": str(rec.get("abstract", "") or "").strip(),
        "journal": str(rec.get("journal", "") or "").strip(),
        "url": str(rec.get("url", "") or "").strip(),
        "source": "websearch",
        "verified": False,
        "citations": 0,
        "open_access": False,
    }


def _dedup_by_title(records: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for r in records:
        k = _norm_title(r.get("title", ""))
        if k and k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _crossref_fill(rec: dict) -> dict:
    """Best-effort keyless Crossref fill of DOI/authors/year/journal by title."""
    import requests
    title = rec.get("title", "")
    if not title or rec.get("doi"):
        return rec
    try:
        r = requests.get(
            "https://api.crossref.org/works",
            params={"query.bibliographic": title, "rows": 1, "mailto": _CONTACT},
            timeout=20,
        )
        if not r.ok:
            return rec
        items = r.json().get("message", {}).get("items", [])
        if not items:
            return rec
        it = items[0]
        # Only trust the match if the returned title is clearly the same work.
        if _title_sim(title, " ".join(it.get("title") or [])) < 0.6:
            return rec
        if it.get("DOI"):
            rec["doi"] = f"https://doi.org/{it['DOI']}"
        if not rec.get("authors") and it.get("author"):
            rec["authors"] = ", ".join(
                f"{a.get('given', '').strip()} {a.get('family', '').strip()}".strip()
                for a in it["author"] if (a.get("family") or a.get("given")))
        if not rec.get("year"):
            dp = (it.get("issued", {}).get("date-parts") or [[None]])[0]
            if dp and dp[0]:
                rec["year"] = str(dp[0])
        if not rec.get("journal"):
            rec["journal"] = " ".join(it.get("container-title") or [])
    except Exception:
        pass
    return rec


def write_ris(records: list[dict], path: Path) -> None:
    """Emit a normalized multi-line RIS sibling for the channel output."""
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


def ingest(results: list[dict], output: Path, enrich: bool = True) -> list[dict]:
    """Normalize -> dedup -> (optional keyless Crossref fill) -> write JSON + .ris."""
    records = [c for c in (_coerce(r) for r in (results or [])) if c]
    records = _dedup_by_title(records)
    if enrich:
        for r in records[:80]:  # bound the polite-pool calls
            _crossref_fill(r)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    write_ris(records, output.with_suffix(".ris"))
    return records


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Normalize agent web-search hits into pipeline JSON (Stage 4d).")
    ap.add_argument("--results", required=True,
                    help="JSON array of agent-gathered candidate papers (or {papers:[...]})")
    ap.add_argument("-o", "--output", default="stage4d_websearch.json", help="Output JSON path")
    ap.add_argument("--no-enrich", action="store_true",
                    help="Skip the best-effort keyless Crossref DOI fill")
    args = ap.parse_args()

    p = Path(args.results)
    if not p.is_file():
        print(f"Error: results file not found: {p}")
        sys.exit(1)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Error: could not parse {p}: {e}")
        sys.exit(1)
    if isinstance(data, dict):  # tolerate a wrapper object
        data = data.get("papers") or data.get("results") or data.get("candidates") or []
    if not isinstance(data, list):
        print("Error: results JSON must be an array (or {\"papers\": [...]}).")
        sys.exit(1)

    res = ingest(data, Path(args.output), enrich=not args.no_enrich)
    print(f"[WEBSEARCH] Ingested {len(res)} unique papers -> {args.output}")


if __name__ == "__main__":
    main()
