#!/usr/bin/env python3
"""
Stage 5 — Merge and deduplicate lit review pipeline outputs.

Two-pass dedup:
  Pass 1: Exact DOI match
  Pass 2: LLM fuzzy match (DeepSeek or Claude) with async concurrency

Usage:
    python lit_dedup.py --inputs stage1.json stage2.json stage4.json -o merged.json
    python lit_dedup.py --input-dir ./results/ -o merged.json --no-llm
    python lit_dedup.py --inputs *.json -o merged.json --yes

Requires: pip install aiohttp
API keys: DEEPSEEK_API_KEY (primary) or ANTHROPIC_API_KEY (fallback)
"""

import argparse
import asyncio
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Load ~/.lit-review-pipeline.env if present (portable key store), matching the
# other entry points so a standalone dedup run still finds the DEEPSEEK/ANTHROPIC
# keys rather than relying solely on inherited environment variables.
_env_file = Path.home() / ".lit-review-pipeline.env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file, override=False)
    except ImportError:
        pass


# Polite-pool contact sent to OpenAlex/Crossref (courtesy only). Override via the
# LITREVIEW_CONTACT_EMAIL env var; falls back to a neutral placeholder.
_OA_MAILTO = os.environ.get("LITREVIEW_CONTACT_EMAIL", "litreview-bot@example.com")


def _oa_auth_params() -> dict[str, str]:
    """Return OpenAlex auth params: api_key if set, else mailto (polite pool)."""
    api_key = os.environ.get("OPENALEX_API_KEY", "")
    if api_key:
        return {"api_key": api_key}
    return {"mailto": _OA_MAILTO}


def _parse_ris_single_line(line: str) -> dict | None:
    """Parse a single-line RIS record (Undermind Report Writer format).

    Format: TY - GEN ID - Bro21 TI - Title Here PY - 2021 ER -
    """
    if "TI - " not in line:
        return None

    paper: dict[str, str] = {}

    # Extract title
    ti_idx = line.index("TI - ") + 5
    end_markers = [" PY - ", " AU - ", " DO - ", " JO - ", " AB - ", " ER -"]
    ti_end = len(line)
    for marker in end_markers:
        pos = line.find(marker, ti_idx)
        if pos != -1 and pos < ti_end:
            ti_end = pos
    paper["title"] = line[ti_idx:ti_end].strip()

    # Extract year
    py_idx = line.find(" PY - ")
    if py_idx != -1:
        py_start = py_idx + 6
        py_end = py_start
        while py_end < len(line) and line[py_end].isdigit():
            py_end += 1
        paper["year"] = line[py_start:py_end].strip()

    # Extract authors
    au_idx = line.find(" AU - ")
    if au_idx != -1:
        au_start = au_idx + 6
        au_end = len(line)
        for marker in [" TI - ", " PY - ", " DO - ", " JO - ", " ER -"]:
            pos = line.find(marker, au_start)
            if pos != -1 and pos < au_end:
                au_end = pos
        paper["authors"] = line[au_start:au_end].strip()

    # Extract DOI
    do_idx = line.find(" DO - ")
    if do_idx != -1:
        do_start = do_idx + 6
        do_end = len(line)
        for marker in [" TI - ", " PY - ", " AU - ", " JO - ", " AB - ", " ER -"]:
            pos = line.find(marker, do_start)
            if pos != -1 and pos < do_end:
                do_end = pos
        doi = line[do_start:do_end].strip()
        if doi and not doi.startswith("http"):
            doi = f"https://doi.org/{doi}"
        paper["doi"] = doi
        paper.setdefault("url", doi)

    return paper if paper.get("title") else None


def _parse_ris(text: str) -> list[dict]:
    """Parse RIS format text into paper dicts (same schema as JSON pipeline).

    Handles two formats:
    1. Standard multi-line RIS (one tag per line with 'TAG  - VALUE')
    2. Undermind single-line RIS ('TY - GEN ID - X TI - Title PY - Year ER -')
    """
    # Detect format
    has_standard = "TY  - " in text
    has_single_line = "TY - GEN" in text and "TY  - " not in text

    if has_single_line:
        papers: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith("TY - "):
                continue
            paper = _parse_ris_single_line(line)
            if paper:
                papers.append(paper)
        return papers

    # Standard multi-line RIS format
    papers = []
    current: dict[str, str] = {}
    authors: list[str] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        if line == "ER  -":
            if current.get("title"):
                current["authors"] = ", ".join(authors)
                doi = current.get("doi", "")
                if doi and not doi.startswith("http"):
                    doi = f"https://doi.org/{doi}"
                    current["doi"] = doi
                current.setdefault("url", doi)
                papers.append(current)
            current = {}
            authors = []
            continue

        # Parse "TAG  - VALUE" format
        if len(line) > 6 and line[2:6] == "  - ":
            tag = line[:2]
            value = line[6:]
        else:
            continue

        if tag in ("T1", "TI"):
            current["title"] = value
        elif tag == "AU":
            authors.append(value)
        elif tag == "JO":
            current["journal"] = value
        elif tag == "PY":
            current["year"] = value
        elif tag == "DO":
            current["doi"] = value
        elif tag == "AB":
            current.setdefault("abstract", "")
            current["abstract"] += (" " + value) if current["abstract"] else value
        elif tag == "UR":
            current["url"] = value

    return papers

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore[assignment]


# ── Utility ─────────────────────────────────────────────────────────────────

def _norm_doi(doi: str) -> str:
    """Normalize DOI for deduplication."""
    if not doi:
        return ""
    doi = re.sub(r'^https?://doi\.org/', '', doi)
    return doi.lower().strip().rstrip("/.")


def _norm_title(title: str) -> str:
    """Normalize title for deduplication."""
    return re.sub(r'\s+', ' ', title.lower().strip()) if title else ""


# ── Source Detection ────────────────────────────────────────────────────────

STAGE4_SOURCES = {"ssrn", "nber", "citation_chain", "forthcoming"}


def _detect_source(paper: dict, filename_stem: str) -> str:
    """Infer pipeline stage/source from a paper record."""
    source = paper.get("source", "")
    if source in STAGE4_SOURCES:
        return source

    # Stage 1 (Undermind): has score with "%" but no source field
    score = str(paper.get("score", ""))
    if "%" in score and not source:
        return "undermind"

    # Fall back to filename stem, normalizing undermind variants
    fallback = source if source else filename_stem
    if "undermind" in fallback:
        return "undermind"
    return fallback


# ── Priority Merge ──────────────────────────────────────────────────────────

_WORKING_PAPER_KEYWORDS = {"working paper", "ssrn", "nber", "discussion paper"}
_FORTHCOMING_KEYWORDS = {"forthcoming", "accepted", "in press"}


def _record_tier(paper: dict) -> int:
    """Score record priority: lower = better.
    0 = published journal, 1 = forthcoming, 2 = working paper.
    """
    journal = (paper.get("journal") or "").lower()
    source = (paper.get("source") or "").lower()

    if any(kw in journal or kw in source for kw in _WORKING_PAPER_KEYWORDS):
        return 2
    if any(kw in journal or kw in source for kw in _FORTHCOMING_KEYWORDS):
        return 1
    return 0


def _record_richness(paper: dict) -> tuple[int, int]:
    """Tie-break: (abstract length, number of filled fields)."""
    abstract_len = len(paper.get("abstract") or "")
    filled = sum(1 for v in paper.values() if v)
    return (abstract_len, filled)


def _pick_winner(records: list[dict]) -> dict:
    """Pick the best record from a group of duplicates."""
    records.sort(key=lambda p: (_record_tier(p), (-_record_richness(p)[0], -_record_richness(p)[1])))
    return records[0]


def _merge_group(records: list[dict]) -> dict:
    """Merge a group of duplicate records into one."""
    winner = _pick_winner(records)
    merged = dict(winner)

    # Collect all sources
    all_sources = []
    seen = set()
    for r in records:
        src = r.get("_detected_source", r.get("source", ""))
        if src and src not in seen:
            seen.add(src)
            all_sources.append(src)
    merged["sources"] = all_sources

    # Collect alt_url: if winner is published and a working paper URL exists
    winner_url = merged.get("url", "")
    alt_urls = []
    for r in records:
        r_url = r.get("url", "")
        if r_url and r_url != winner_url:
            alt_urls.append(r_url)
    merged["alt_url"] = alt_urls[0] if alt_urls else ""

    # Clean up internal field
    merged.pop("_detected_source", None)

    return merged


# ── Pass 1: DOI Dedup ───────────────────────────────────────────────────────

def _doi_dedup(papers: list[dict]) -> tuple[list[dict], list[dict[str, object]]]:
    """Group papers by normalized DOI. Returns (deduped_list, merge_log)."""
    doi_groups: dict[str, list[dict]] = {}
    no_doi: list[dict] = []

    for p in papers:
        doi = _norm_doi(p.get("doi", ""))
        if doi:
            doi_groups.setdefault(doi, []).append(p)
        else:
            no_doi.append(p)

    deduped = []
    merge_log = []

    for doi, group in doi_groups.items():
        if len(group) == 1:
            merged = dict(group[0])
            src = merged.pop("_detected_source", merged.get("source", ""))
            merged["sources"] = [src] if src else []
            merged["alt_url"] = ""
            deduped.append(merged)
        else:
            merged = _merge_group(group)
            deduped.append(merged)
            merge_log.append({
                "doi": doi,
                "merged_records": [
                    {"title": r.get("title", ""), "source": r.get("_detected_source", r.get("source", ""))}
                    for r in group
                ],
                "winner": {"title": merged["title"], "sources": merged["sources"]},
            })

    # Papers without DOI pass through
    for p in no_doi:
        entry = dict(p)
        src = entry.pop("_detected_source", entry.get("source", ""))
        entry["sources"] = [src] if src else []
        entry["alt_url"] = ""
        deduped.append(entry)

    return deduped, merge_log


# ── Pass 2: LLM Fuzzy Match ────────────────────────────────────────────────

def _should_compare(a: dict, b: dict) -> bool:
    """Pre-filter: only compare pairs with author surname overlap or title Jaccard > 0.4."""
    # Check author surname overlap
    surnames_a = {s.strip().split()[-1].lower() for s in (a.get("authors") or "").split(",") if s.strip()}
    surnames_b = {s.strip().split()[-1].lower() for s in (b.get("authors") or "").split(",") if s.strip()}
    if surnames_a and surnames_b and (surnames_a & surnames_b):
        return True

    # Check title word overlap (Jaccard > 0.4)
    words_a = set(re.sub(r'[^\w\s]', '', (a.get("title") or "").lower()).split())
    words_b = set(re.sub(r'[^\w\s]', '', (b.get("title") or "").lower()).split())
    words_a.discard("")
    words_b.discard("")
    if words_a and words_b:
        jaccard = len(words_a & words_b) / len(words_a | words_b)
        if jaccard > 0.4:
            return True

    return False


def _build_candidate_pairs(papers: list[dict]) -> list[tuple[int, int]]:
    """Build list of (i, j) pairs that pass pre-filtering."""
    pairs = []
    n = len(papers)
    for i in range(n):
        for j in range(i + 1, n):
            if _should_compare(papers[i], papers[j]):
                pairs.append((i, j))
    return pairs


def _format_paper_for_llm(p: dict) -> str:
    """Format a paper record for the LLM prompt."""
    lines = []
    if p.get("title"):
        lines.append(f"Title: {p['title']}")
    if p.get("authors"):
        lines.append(f"Authors: {p['authors']}")
    if p.get("year"):
        lines.append(f"Year: {p['year']}")
    if p.get("journal"):
        lines.append(f"Journal: {p['journal']}")
    if p.get("doi"):
        lines.append(f"DOI: {p['doi']}")
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "You are deduplicating academic papers. Given two paper records, decide "
    "whether they are the same paper (possibly different versions or venues).\n\n"
    "Respond in EXACTLY this format (three lines, nothing else):\n"
    "DECISION: yes/no\n"
    "CONFIDENCE: high/medium/low\n"
    "RATIONALE: <one sentence explaining why>"
)


def _parse_llm_response(raw: str) -> tuple[str, str, str]:
    """Parse structured LLM response into (decision, confidence, rationale)."""
    text = raw.strip()
    decision = "no"
    confidence = ""
    rationale = ""

    for line in text.splitlines():
        line_lower = line.strip().lower()
        if line_lower.startswith("decision:"):
            val = line_lower.split(":", 1)[1].strip()
            decision = "yes" if val.startswith("yes") else "no"
        elif line_lower.startswith("confidence:"):
            confidence = line.strip().split(":", 1)[1].strip().lower()
        elif line_lower.startswith("rationale:"):
            rationale = line.strip().split(":", 1)[1].strip()

    # Fallback: if no structured parse, treat first word as decision
    if not confidence and not rationale:
        decision = "yes" if text.lower().startswith("yes") else "no"

    return decision, confidence, rationale


async def _query_one_deepseek(session, sem, pair_data, api_key, model):
    """Query DeepSeek API for one pair."""
    i, j, paper_a, paper_b = pair_data
    async with sem:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"Paper A:\n{_format_paper_for_llm(paper_a)}\n\n"
                    f"Paper B:\n{_format_paper_for_llm(paper_b)}"
                )},
            ],
            "max_tokens": 100,
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with session.post(
                    "https://api.deepseek.com/chat/completions",
                    json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status in (429, 529) or resp.status >= 500:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        return (i, j, "error", "", "", f"HTTP {resp.status}")
                    if resp.status != 200:
                        return (i, j, "error", "", "", f"HTTP {resp.status}")
                    data = await resp.json()
                    raw = data["choices"][0]["message"]["content"].strip()
                    decision, confidence, rationale = _parse_llm_response(raw)
                    return (i, j, decision, confidence, rationale, raw)
            except Exception as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return (i, j, "error", "", "", str(e))
        return (i, j, "error", "", "", "Max retries exceeded")


async def _query_one_anthropic(session, sem, pair_data, api_key, model):
    """Query Anthropic API for one pair."""
    i, j, paper_a, paper_b = pair_data
    async with sem:
        body = {
            "model": model,
            "max_tokens": 100,
            "system": SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": (
                    f"Paper A:\n{_format_paper_for_llm(paper_a)}\n\n"
                    f"Paper B:\n{_format_paper_for_llm(paper_b)}"
                )},
            ],
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with session.post(
                    "https://api.anthropic.com/v1/messages",
                    json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status in (429, 529) or resp.status >= 500:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        return (i, j, "error", "", "", f"HTTP {resp.status}")
                    if resp.status != 200:
                        return (i, j, "error", "", "", f"HTTP {resp.status}")
                    data = await resp.json()
                    raw = data["content"][0]["text"].strip()
                    decision, confidence, rationale = _parse_llm_response(raw)
                    return (i, j, decision, confidence, rationale, raw)
            except Exception as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return (i, j, "error", "", "", str(e))
        return (i, j, "error", "", "", "Max retries exceeded")


async def _query_llm_batch(papers, pairs, api_key, model, concurrency, use_anthropic=False):
    """Send all pairs to LLM concurrently, return results."""
    sem = asyncio.Semaphore(concurrency)

    pair_data = [(i, j, papers[i], papers[j]) for i, j in pairs]
    query_fn = _query_one_anthropic if use_anthropic else _query_one_deepseek

    async with aiohttp.ClientSession() as session:
        tasks = [query_fn(session, sem, pd, api_key, model) for pd in pair_data]
        return await asyncio.gather(*tasks)


def _paper_summary(p: dict) -> dict:
    """Extract key fields for the merge log / report."""
    return {
        "title": p.get("title", ""),
        "authors": p.get("authors", ""),
        "year": p.get("year", ""),
        "doi": p.get("doi", ""),
        "journal": p.get("journal", ""),
        "source": p.get("_detected_source", p.get("source", "")),
    }


def _merge_from_results(papers: list[dict],
                        results: list) -> tuple[list[dict], list[dict[str, object]]]:
    """Union-find merge from per-pair LLM results.

    ``results`` is an iterable of ``(i, j, decision, confidence, rationale, raw)``
    tuples. Returns ``(deduped, merge_log)``. Shared by the in-script LLM pass and
    the agent-driven ``--ingest-verdicts`` path so both merge identically.
    """
    parent = list(range(len(papers)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    merge_log = []
    for result in results:
        i, j, decision, confidence, rationale, raw = result
        if decision == "yes":
            union(i, j)
        merge_log.append({
            "paper_a": _paper_summary(papers[i]),
            "paper_b": _paper_summary(papers[j]),
            "decision": decision,
            "confidence": confidence,
            "rationale": rationale,
        })

    # Group by connected component
    groups: dict[int, list[int]] = {}
    for idx in range(len(papers)):
        root = find(idx)
        groups.setdefault(root, []).append(idx)

    deduped = []
    for indices in groups.values():
        group = [papers[idx] for idx in indices]
        if len(group) == 1:
            deduped.append(group[0])
        else:
            merged = _merge_group(group)
            deduped.append(merged)
            # Tag merge_log entries with the winner
            group_titles = {g.get("title") for g in group}
            for entry in merge_log:
                if entry["decision"] == "yes" and entry["paper_a"]["title"] in group_titles:
                    entry["merged_into"] = merged.get("title", "")

    return deduped, merge_log


def _llm_dedup(papers: list[dict], api_key: str, model: str, concurrency: int,
               use_anthropic: bool = False) -> tuple[list[dict], list[dict[str, object]], int]:
    """Pass 2: LLM-based fuzzy dedup. Returns (deduped, merge_log, pairs_checked)."""
    pairs = _build_candidate_pairs(papers)
    if not pairs:
        return papers, [], 0

    print(f"  LLM pass: {len(pairs)} candidate pairs to check...")

    # Run async queries, then merge by connected component.
    results = asyncio.run(_query_llm_batch(papers, pairs, api_key, model, concurrency, use_anthropic))
    deduped, merge_log = _merge_from_results(papers, results)
    return deduped, merge_log, len(pairs)


# ── RIS Writer ──────────────────────────────────────────────────────────────

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


# ── Dedup Report ────────────────────────────────────────────────────────────

def _field_or_na(val: str) -> str:
    return val if val else "n/a"


def write_dedup_report(llm_merge_log: list[dict], doi_merge_log: list[dict],
                       stats: dict, path: Path) -> None:
    """Write a human-readable markdown report of all merge decisions."""
    lines = ["# Dedup Report", ""]

    # Stats summary
    lines.append("## Summary")
    lines.append("")
    lines.append(f"| Metric | Count |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total input papers | {stats['total_input']} |")
    lines.append(f"| After DOI dedup | {stats['after_doi_dedup']} |")
    lines.append(f"| LLM candidate pairs checked | {stats['llm_pairs_checked']} |")
    lines.append(f"| After LLM dedup | {stats['after_llm_dedup']} |")
    lines.append(f"| **Total duplicates removed** | **{stats['total_input'] - stats['after_llm_dedup']}** |")
    lines.append("")

    # LLM merges (the main section for review)
    yes_entries = [e for e in llm_merge_log if e.get("decision") == "yes"]
    no_entries = [e for e in llm_merge_log if e.get("decision") == "no"]

    lines.append(f"## LLM Merges ({len(yes_entries)} merged, {len(no_entries)} kept separate)")
    lines.append("")

    if yes_entries:
        lines.append("### Merged Pairs")
        lines.append("")
        for idx, entry in enumerate(yes_entries, 1):
            a = entry["paper_a"]
            b = entry["paper_b"]
            confidence = entry.get("confidence", "")
            rationale = entry.get("rationale", "")
            merged_into = entry.get("merged_into", "")

            lines.append(f"#### Merge {idx}  —  Confidence: **{confidence or 'n/a'}**")
            lines.append("")
            lines.append("| | Paper A | Paper B |")
            lines.append("|---|---|---|")
            lines.append(f"| **Title** | {_field_or_na(a.get('title', ''))} | {_field_or_na(b.get('title', ''))} |")
            lines.append(f"| **Authors** | {_field_or_na(a.get('authors', ''))} | {_field_or_na(b.get('authors', ''))} |")
            lines.append(f"| **Year** | {_field_or_na(a.get('year', ''))} | {_field_or_na(b.get('year', ''))} |")
            lines.append(f"| **Journal** | {_field_or_na(a.get('journal', ''))} | {_field_or_na(b.get('journal', ''))} |")
            lines.append(f"| **DOI** | {_field_or_na(a.get('doi', ''))} | {_field_or_na(b.get('doi', ''))} |")
            lines.append(f"| **Source** | {_field_or_na(a.get('source', ''))} | {_field_or_na(b.get('source', ''))} |")
            lines.append("")
            lines.append(f"**Rationale:** {rationale or 'n/a'}")
            if merged_into:
                lines.append(f"**Kept as:** {merged_into}")
            lines.append("")
            lines.append("---")
            lines.append("")

    # Rejected pairs (kept separate)
    if no_entries:
        lines.append("### Kept Separate (LLM said no)")
        lines.append("")
        lines.append("| # | Paper A | Paper B | Confidence | Rationale |")
        lines.append("|---|---------|---------|------------|-----------|")
        for idx, entry in enumerate(no_entries, 1):
            a_title = entry["paper_a"].get("title", "")[:60]
            b_title = entry["paper_b"].get("title", "")[:60]
            conf = entry.get("confidence", "")
            rat = entry.get("rationale", "")[:80]
            lines.append(f"| {idx} | {a_title} | {b_title} | {conf} | {rat} |")
        lines.append("")

    # DOI merges (for completeness)
    if doi_merge_log:
        lines.append(f"## DOI Merges ({len(doi_merge_log)})")
        lines.append("")
        lines.append("| DOI | Records Merged | Winner |")
        lines.append("|-----|---------------|--------|")
        for entry in doi_merge_log:
            doi = entry.get("doi", "")
            records = entry.get("merged_records", [])
            sources = ", ".join(r.get("source", "") for r in records)
            winner_title = entry.get("winner", {}).get("title", "")[:60]
            lines.append(f"| `{doi}` | {sources} | {winner_title} |")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def _reconstruct_abstract(inverted_index: dict | None) -> str:
    """Reconstruct abstract text from OpenAlex inverted index format."""
    if not inverted_index or not isinstance(inverted_index, dict):
        return ""
    word_positions: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(w for _, w in word_positions)


def _normalize_title(t: str) -> str:
    """Lowercase, strip diacritics, and turn punctuation into spaces so title
    comparisons survive formatting, accent, and subtitle/punctuation variants."""
    t = unicodedata.normalize("NFKD", t or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _title_similarity(a: str, b: str) -> float:
    """Jaccard similarity on normalized word sets."""
    sa, sb = set(_normalize_title(a).split()), set(_normalize_title(b).split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


async def _enrich_all_metadata(papers: list[dict], log_path: Path | None = None) -> dict:
    """Unified metadata enrichment: fill ALL missing fields for every paper.

    For each paper, cascades through OpenAlex → Crossref → Semantic Scholar
    (→ Google Scholar snippet for abstract-only last resort).

    Each source call extracts ALL available fields at once: authors, DOI,
    abstract, journal, year. One API call per source fills everything it can.

    Returns a summary dict with per-field and per-source counts.
    """
    import aiohttp
    import urllib.parse
    from datetime import datetime

    OA_BASE = "https://api.openalex.org"
    CR_BASE = "https://api.crossref.org"
    S2_BASE = "https://api.semanticscholar.org/graph/v1"
    sem = asyncio.Semaphore(5)

    FIELDS = ("authors", "doi", "abstract", "journal", "year", "url")
    stats = {
        "enriched": 0,
        "authors_filled": 0, "dois_filled": 0, "abstracts_filled": 0,
        "journals_filled": 0, "years_filled": 0, "urls_filled": 0,
        "src_openalex": 0, "src_crossref": 0, "src_s2": 0, "src_gs": 0,
        "not_found": 0, "errors": 0,
    }
    log_entries: list[str] = []

    def _log(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        log_entries.append(f"[{ts}] {msg}")

    def _fill(paper: dict, *, authors: str = "", doi: str = "",
              abstract: str = "", journal: str = "", year: str = "",
              url: str = "") -> list[str]:
        """Fill missing fields. Returns list of field names that were filled."""
        filled = []
        if authors and not (paper.get("authors") or "").strip():
            paper["authors"] = authors
            filled.append("authors")
        if doi and not (paper.get("doi") or "").strip():
            paper["doi"] = doi
            filled.append("doi")
        if abstract and not (paper.get("abstract") or "").strip():
            paper["abstract"] = abstract
            filled.append("abstract")
        if journal and not (paper.get("journal") or "").strip():
            paper["journal"] = journal
            filled.append("journal")
        if year and not (paper.get("year") or "").strip():
            paper["year"] = year
            filled.append("year")
        if url and not (paper.get("url") or "").strip():
            paper["url"] = url
            filled.append("url")
        return filled

    def _missing(paper: dict) -> set[str]:
        return {f for f in FIELDS if not (paper.get(f) or "").strip()}

    # ── Source functions (each extracts ALL available fields) ──────────

    async def _try_openalex(session: aiohttp.ClientSession, paper: dict,
                            title: str, doi_bare: str | None) -> list[str]:
        """Try OpenAlex by DOI first, then by title. Returns filled fields."""
        # DOI path (fast, exact)
        if doi_bare:
            url = f"{OA_BASE}/works/https://doi.org/{urllib.parse.quote(doi_bare)}"
            async with sem:
                try:
                    async with session.get(
                        url, params={**_oa_auth_params(),
                                     "select": "title,authorships,doi,primary_location,publication_year,abstract_inverted_index"},
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return _extract_openalex(paper, data)
                except Exception:
                    pass

        # Title search path
        url = f"{OA_BASE}/works"
        params = {
            **_oa_auth_params(),
            "filter": f'title.search:"{title}"',
            "select": "title,authorships,doi,primary_location,publication_year,abstract_inverted_index",
            "per_page": "3",
        }
        async with sem:
            try:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
            except Exception:
                return []

        title_lower = title.lower()
        for r in data.get("results", []):
            if _title_similarity(title_lower, (r.get("title") or "").lower()) < 0.75:
                continue
            filled = _extract_openalex(paper, r)
            if filled:
                return filled
        return []

    def _extract_openalex(paper: dict, r: dict) -> list[str]:
        auths = r.get("authorships", [])
        names = [a.get("author", {}).get("display_name", "") for a in auths]
        names = [n for n in names if n]
        loc = (r.get("primary_location") or {})
        src = (loc.get("source") or {})
        abstract = _reconstruct_abstract(r.get("abstract_inverted_index"))
        doi = r.get("doi", "")
        return _fill(
            paper,
            authors=", ".join(names) if names else "",
            doi=doi,
            abstract=abstract,
            journal=src.get("display_name", ""),
            year=str(r.get("publication_year", "") or ""),
            url=doi or "",
        )

    async def _try_crossref(session: aiohttp.ClientSession, paper: dict,
                             title: str, doi_bare: str | None) -> list[str]:
        # DOI path
        if doi_bare:
            url = f"{CR_BASE}/works/{urllib.parse.quote(doi_bare)}"
            async with sem:
                try:
                    async with session.get(
                        url,
                        headers={"User-Agent": f"LitReviewPipeline/1.0 (mailto:{_OA_MAILTO})"},
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            filled = _extract_crossref(paper, data.get("message", {}))
                            if filled:
                                return filled
                except Exception:
                    pass

        # Title search path
        url = f"{CR_BASE}/works"
        params = {"query.title": title, "rows": "3"}
        async with sem:
            try:
                async with session.get(
                    url, params=params,
                    headers={"User-Agent": f"LitReviewPipeline/1.0 (mailto:{_OA_MAILTO})"},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
            except Exception:
                return []

        title_lower = title.lower()
        for item in data.get("message", {}).get("items", []):
            titles = item.get("title", [])
            if not titles or _title_similarity(title_lower, titles[0].lower()) < 0.75:
                continue
            filled = _extract_crossref(paper, item)
            if filled:
                return filled
        return []

    def _extract_crossref(paper: dict, item: dict) -> list[str]:
        cr_authors = item.get("author", [])
        names = []
        for a in cr_authors:
            given = a.get("given", "")
            family = a.get("family", "")
            if family:
                names.append(f"{given} {family}".strip())
        abstract = re.sub(r"<[^>]+>", "", item.get("abstract", "")).strip()
        ct = item.get("container-title", [])
        journal = ct[0] if ct else ""
        doi = item.get("DOI", "")
        if doi and not doi.startswith("http"):
            doi = f"https://doi.org/{doi}"
        yr = ""
        dp = item.get("published", {}).get("date-parts", [[]])
        if dp and dp[0]:
            yr = str(dp[0][0])
        return _fill(
            paper,
            authors=", ".join(names) if names else "",
            doi=doi,
            abstract=abstract if len(abstract) > 50 else "",
            journal=journal,
            year=yr,
            url=doi or "",
        )

    async def _try_semantic_scholar(session: aiohttp.ClientSession, paper: dict,
                                     title: str, doi_bare: str | None) -> list[str]:
        # DOI path
        if doi_bare:
            url = f"{S2_BASE}/paper/DOI:{urllib.parse.quote(doi_bare)}"
            async with sem:
                try:
                    async with session.get(
                        url, params={"fields": "title,authors,externalIds,abstract,journal,year"},
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            filled = _extract_s2(paper, data)
                            if filled:
                                return filled
                except Exception:
                    pass

        # Title search path
        url = f"{S2_BASE}/paper/search"
        params = {"query": title, "fields": "title,authors,externalIds,abstract,journal,year",
                  "limit": "3"}
        async with sem:
            try:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
            except Exception:
                return []

        title_lower = title.lower()
        for r in data.get("data", []):
            if _title_similarity(title_lower, (r.get("title") or "").lower()) < 0.75:
                continue
            filled = _extract_s2(paper, r)
            if filled:
                return filled
        return []

    def _extract_s2(paper: dict, r: dict) -> list[str]:
        s2_authors = r.get("authors", [])
        names = [a.get("name", "") for a in s2_authors if a.get("name")]
        ext = r.get("externalIds") or {}
        doi = ext.get("DOI", "")
        if doi and not doi.startswith("http"):
            doi = f"https://doi.org/{doi}"
        journal = ""
        if r.get("journal"):
            journal = r["journal"].get("name", "")
        return _fill(
            paper,
            authors=", ".join(names) if names else "",
            doi=doi,
            abstract=r.get("abstract", "") or "",
            journal=journal,
            year=str(r.get("year", "") or ""),
            url=doi or "",
        )

    async def _try_gs_snippet(session: aiohttp.ClientSession, paper: dict,
                               title: str) -> list[str]:
        """Last resort: GS snippet for abstract only."""
        searchapi_key = os.environ.get("SEARCHAPI_API_KEY", "")
        if not searchapi_key:
            return []
        query = f'"{title}"'
        url = "https://www.searchapi.io/api/v1/search"
        params = {"engine": "google_scholar", "q": query,
                  "api_key": searchapi_key, "num": "3"}
        async with sem:
            try:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
            except Exception:
                return []

        title_lower = title.lower()
        for r in data.get("organic_results", []):
            if _title_similarity(title_lower, (r.get("title") or "").lower()) < 0.7:
                continue
            snippet = (r.get("snippet") or "").strip()
            if snippet and len(snippet) > 30 and not (paper.get("abstract") or "").strip():
                paper["abstract"] = snippet
                paper["_abstract_source"] = "google_scholar_snippet"
                return ["abstract"]
        return []

    # ── Per-paper cascade ─────────────────────────────────────────────

    completed = 0

    async def _enrich_one(session: aiohttp.ClientSession, paper: dict) -> str:
        """Run full cascade. Returns primary source that contributed."""
        nonlocal completed
        title = (paper.get("title") or "").strip()
        if not title or len(title) < 10:
            completed += 1
            return "skip"

        doi_raw = (paper.get("doi") or "").strip()
        doi_bare = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi_raw) if doi_raw else None

        try:
            before = _missing(paper)

            # Source 1: OpenAlex
            oa_filled = await _try_openalex(session, paper, title, doi_bare)
            if oa_filled:
                doi_raw = (paper.get("doi") or "").strip()
                doi_bare = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi_raw) if doi_raw else doi_bare

            # Source 2: Crossref (fills remaining gaps)
            cr_filled = []
            if _missing(paper):
                cr_filled = await _try_crossref(session, paper, title, doi_bare)
                if cr_filled:
                    doi_raw = (paper.get("doi") or "").strip()
                    doi_bare = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi_raw) if doi_raw else doi_bare

            # Source 3: Semantic Scholar
            s2_filled = []
            if _missing(paper):
                s2_filled = await _try_semantic_scholar(session, paper, title, doi_bare)

            # Source 4: GS snippet (abstract last resort only)
            gs_filled = []
            if not (paper.get("abstract") or "").strip():
                gs_filled = await _try_gs_snippet(session, paper, title)

            after = _missing(paper)
            any_filled = before != after

            completed += 1
            if oa_filled:
                return "openalex"
            elif cr_filled:
                return "crossref"
            elif s2_filled:
                return "s2"
            elif gs_filled:
                return "gs"
            elif any_filled:
                return "mixed"
            else:
                return "not_found"
        except Exception as e:
            completed += 1
            _log(f"ERROR: {title[:60]}: {e}")
            return "error"

    # ── Run with progress ─────────────────────────────────────────────

    _log(f"Metadata enrichment: {len(papers)} papers to process")

    async with aiohttp.ClientSession() as session:
        total = len(papers)

        async def _with_progress(idx: int, paper: dict):
            result = await _enrich_one(session, paper)
            if (idx + 1) % 25 == 0 or idx + 1 == total:
                _log(f"  [{idx+1}/{total}] processed")
                print(f"    [{idx+1}/{total}] enriched")
            return result

        results = await asyncio.gather(
            *[_with_progress(i, p) for i, p in enumerate(papers)],
            return_exceptions=True,
        )

    # ── Tally ─────────────────────────────────────────────────────────
    for r in results:
        if isinstance(r, Exception):
            stats["errors"] += 1
            continue
        if r == "openalex":
            stats["src_openalex"] += 1
            stats["enriched"] += 1
        elif r == "crossref":
            stats["src_crossref"] += 1
            stats["enriched"] += 1
        elif r == "s2":
            stats["src_s2"] += 1
            stats["enriched"] += 1
        elif r == "gs":
            stats["src_gs"] += 1
            stats["enriched"] += 1
        elif r == "mixed":
            stats["enriched"] += 1
        elif r == "not_found":
            stats["not_found"] += 1
        elif r == "error":
            stats["errors"] += 1

    # Count per-field fills by comparing to what we expect
    for p in papers:
        if (p.get("authors") or "").strip():
            stats["authors_filled"] += 1  # will over-count; fix below
        if (p.get("doi") or "").strip():
            stats["dois_filled"] += 1
        if (p.get("abstract") or "").strip():
            stats["abstracts_filled"] += 1
        if (p.get("journal") or "").strip():
            stats["journals_filled"] += 1
    # These are totals AFTER enrichment, not deltas — fine for reporting

    _log(f"Enrichment complete: {stats['enriched']}/{len(papers)} improved")

    if log_path:
        try:
            log_path.write_text("\n".join(log_entries), encoding="utf-8")
        except Exception:
            pass

    return stats


async def _enrich_dois(papers: list[dict], log_path: Path | None = None) -> dict:
    """Fill missing DOIs via OpenAlex → Crossref → Semantic Scholar cascade.

    Mutates papers in-place. Only processes papers whose DOI is empty/missing.
    Returns a summary dict with counts per source and failure details.
    """
    import aiohttp
    import urllib.parse
    from datetime import datetime

    OA_BASE = "https://api.openalex.org"
    CR_BASE = "https://api.crossref.org"
    S2_BASE = "https://api.semanticscholar.org/graph/v1"
    sem = asyncio.Semaphore(5)

    # Only process papers that actually need DOIs
    to_enrich = [
        p for p in papers
        if (p.get("title") or "").strip() and len((p.get("title") or "").strip()) >= 10
        and not (p.get("doi") or "").strip()
    ]
    skipped = len(papers) - len(to_enrich)

    # Tracking
    stats = {"openalex": 0, "crossref": 0, "semantic_scholar": 0,
             "not_found": 0, "error": 0, "skipped": skipped, "total": len(papers)}
    log_entries: list[str] = []

    def _log(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        log_entries.append(f"[{ts}] {msg}")

    _log(f"DOI enrichment: {len(to_enrich)} papers to process, {skipped} skipped (short title or already has DOI)")

    # ── Source functions ──────────────────────────────────────────────

    async def _try_openalex(session: aiohttp.ClientSession, title: str) -> str | None:
        url = f"{OA_BASE}/works"
        params = {**_oa_auth_params(), "filter": f'title.search:"{title}"',
                  "select": "title,doi", "per_page": "3"}
        async with sem:
            try:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            except Exception:
                return None
        title_lower = title.lower()
        for r in data.get("results", []):
            if _title_similarity(title_lower, (r.get("title") or "").lower()) < 0.75:
                continue
            doi = r.get("doi", "")
            if doi:
                return doi
        return None

    async def _try_crossref(session: aiohttp.ClientSession, title: str) -> str | None:
        url = f"{CR_BASE}/works"
        params = {"query.title": title, "rows": "3", "select": "DOI,title"}
        async with sem:
            try:
                async with session.get(
                    url, params=params,
                    headers={"User-Agent": f"LitReviewPipeline/1.0 (mailto:{_OA_MAILTO})"},
                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            except Exception:
                return None
        title_lower = title.lower()
        for item in data.get("message", {}).get("items", []):
            titles = item.get("title", [])
            if not titles or _title_similarity(title_lower, titles[0].lower()) < 0.75:
                continue
            doi = item.get("DOI", "")
            if doi:
                return f"https://doi.org/{doi}" if not doi.startswith("http") else doi
        return None

    async def _try_semantic_scholar(session: aiohttp.ClientSession, title: str) -> str | None:
        url = f"{S2_BASE}/paper/search"
        params = {"query": title, "fields": "title,externalIds", "limit": "3"}
        async with sem:
            try:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            except Exception:
                return None
        title_lower = title.lower()
        for r in data.get("data", []):
            if _title_similarity(title_lower, (r.get("title") or "").lower()) < 0.75:
                continue
            ext = r.get("externalIds") or {}
            doi = ext.get("DOI", "")
            if doi:
                return f"https://doi.org/{doi}"
        return None

    # ── Per-paper cascade ─────────────────────────────────────────────

    completed = 0

    async def _fill_doi(session: aiohttp.ClientSession, paper: dict) -> str:
        """Returns the source name that succeeded, or 'not_found'/'error'."""
        nonlocal completed
        title = (paper.get("title") or "").strip()
        try:
            doi = await _try_openalex(session, title)
            if doi:
                paper["doi"] = doi
                paper.setdefault("url", doi)
                paper["_doi_source"] = "openalex"
                completed += 1
                return "openalex"

            doi = await _try_crossref(session, title)
            if doi:
                paper["doi"] = doi
                paper.setdefault("url", doi)
                paper["_doi_source"] = "crossref"
                completed += 1
                return "crossref"

            doi = await _try_semantic_scholar(session, title)
            if doi:
                paper["doi"] = doi
                paper.setdefault("url", doi)
                paper["_doi_source"] = "semantic_scholar"
                completed += 1
                return "semantic_scholar"

            completed += 1
            return "not_found"
        except Exception as e:
            completed += 1
            _log(f"ERROR: {title[:60]}: {e}")
            return "error"

    # ── Run with progress ─────────────────────────────────────────────

    async with aiohttp.ClientSession() as session:
        tasks = [_fill_doi(session, p) for p in to_enrich]
        total = len(tasks)
        results: list[str] = []

        async def _with_progress(idx: int, coro):
            result = await coro
            if (idx + 1) % 25 == 0 or idx + 1 == total:
                _log(f"  [{idx+1}/{total}] processed")
                print(f"    [{idx+1}/{total}] DOIs processed")
            return result

        results = await asyncio.gather(
            *[_with_progress(i, t) for i, t in enumerate(tasks)],
            return_exceptions=True,
        )

    # ── Tally results ─────────────────────────────────────────────────
    for r in results:
        if isinstance(r, str) and r in stats:
            stats[r] += 1
        elif isinstance(r, Exception):
            stats["error"] += 1

    filled = stats["openalex"] + stats["crossref"] + stats["semantic_scholar"]
    _log(f"DOI enrichment complete: {filled}/{len(to_enrich)} filled")
    _log(f"  OpenAlex: {stats['openalex']}, Crossref: {stats['crossref']}, "
         f"S2: {stats['semantic_scholar']}, Not found: {stats['not_found']}, "
         f"Errors: {stats['error']}")

    # ── Write log ─────────────────────────────────────────────────────
    if log_path:
        try:
            log_path.write_text("\n".join(log_entries), encoding="utf-8")
        except Exception:
            pass

    return stats


# ── Main ────────────────────────────────────────────────────────────────────

def _run_ingest(args) -> None:
    """Agent-driven ingest: merge the subagent pair verdicts and write all dedup outputs.

    Reads an --emit-pairs file (the after-DOI papers + candidate pairs) and the
    subagent verdicts JSON, runs the same union-find merge as the autonomous path,
    and writes JSON / RIS / dedup_log.json / dedup_report.md. No API call.
    """
    emit_path, verdicts_path = args.ingest_verdicts
    emit = json.loads(Path(emit_path).read_text(encoding="utf-8"))
    verdicts = json.loads(Path(verdicts_path).read_text(encoding="utf-8"))

    papers = emit.get("papers", [])
    doi_merge_log = emit.get("doi_merge_log", [])
    meta = emit.get("meta", {})
    emitted = emit.get("pairs", [])
    n = len(papers)

    # The emit file is the source of truth for which pairs were judged. Build a
    # canonical, bounds-checked set of (i, j) we actually asked about so malformed
    # or out-of-range verdicts cannot crash union-find or silently merge papers we
    # never compared.
    allowed: set[tuple[int, int]] = set()
    for p in emitted:
        try:
            pi, pj = int(p["i"]), int(p["j"])
        except (KeyError, ValueError, TypeError):
            continue
        if 0 <= pi < n and 0 <= pj < n and pi != pj:
            allowed.add((min(pi, pj), max(pi, pj)))

    verdict_map: dict[tuple[int, int], tuple] = {}
    ignored = 0
    for v in verdicts:
        try:
            i, j = int(v["i"]), int(v["j"])
        except (KeyError, ValueError, TypeError):
            ignored += 1
            continue
        if not (0 <= i < n and 0 <= j < n) or i == j:
            ignored += 1
            continue
        key = (min(i, j), max(i, j))
        if key not in allowed:
            ignored += 1  # a verdict for a pair we never emitted
            continue
        decision = str(v.get("decision", "no")).strip().lower()
        if decision.startswith("yes"):
            decision = "yes"
        elif decision == "error":
            decision = "error"
        else:
            decision = "no"
        verdict_map[key] = (key[0], key[1], decision, str(v.get("confidence", "")),
                            str(v.get("rationale", "")), "")

    # Every emitted pair gets exactly one result; an emitted pair with no verdict
    # returned is treated as "not a duplicate" rather than dropped.
    results = []
    missing = 0
    for key in sorted(allowed):
        if key in verdict_map:
            results.append(verdict_map[key])
        else:
            results.append((key[0], key[1], "no", "", "no verdict returned", ""))
            missing += 1
    if ignored or missing:
        print(f"[DEDUP] Ingest: ignored {ignored} invalid/unexpected verdict(s); "
              f"{missing} emitted pair(s) had no verdict (treated as non-duplicates)")

    deduped, llm_merge_log = _merge_from_results(papers, results)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(deduped, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[DEDUP] Ingest: {len(papers)} → {len(deduped)} after LLM merge "
          f"({len(papers) - len(deduped)} merged from {len(verdicts)} verdicts)")

    ris_path = output_path.parent / f"{output_path.stem}.ris"
    write_ris(deduped, ris_path)

    stats = {
        "total_input": meta.get("total_input", len(papers)),
        "after_doi_dedup": len(papers),
        "after_llm_dedup": len(deduped),
        "llm_pairs_checked": len(verdicts),
    }
    log_path = output_path.parent / "dedup_log.json"
    log_path.write_text(
        json.dumps({"doi_merges": doi_merge_log, "llm_merges": llm_merge_log, "stats": stats},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    report_path = output_path.parent / "dedup_report.md"
    write_dedup_report(llm_merge_log, doi_merge_log, stats, report_path)
    print(f"[DEDUP] Wrote {output_path.name}, {ris_path.name}, {log_path.name}, {report_path.name}")


async def _verify_all_papers(papers: list[dict], log_path: "Path | None" = None) -> dict:
    """Anti-hallucination check: confirm each paper exists in an authoritative
    index (OpenAlex, Crossref, or Semantic Scholar).

    A paper is verified when (a) its DOI resolves in OpenAlex or Crossref, or
    (b) a title search returns a record whose title matches at >= 0.85, or at
    >= 0.75 with author-surname or year (+/-1) corroboration. Mutates each paper
    in place: sets ``verified`` (bool), ``verified_source``, ``verified_via``.
    Runs over EVERY paper (not only those needing enrichment), since a paper with
    complete metadata still has to be confirmed before it can be trusted.
    """
    import aiohttp
    import urllib.parse
    from datetime import datetime

    OA_BASE = "https://api.openalex.org"
    CR_BASE = "https://api.crossref.org"
    S2_BASE = "https://api.semanticscholar.org/graph/v1"
    sem = asyncio.Semaphore(5)
    stats = {"verified": 0, "unverified": 0, "via_doi": 0, "via_title": 0,
             "src_openalex": 0, "src_crossref": 0, "src_s2": 0, "errors": 0}
    log_entries: list[str] = []

    def _log(msg: str):
        log_entries.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def _year_int(y):
        m = re.search(r"(19|20)\d{2}", str(y or ""))
        return int(m.group(0)) if m else None

    def _surnames(authors: str) -> set:
        out = set()
        for a in re.split(r"[;,]| and ", authors or ""):
            toks = a.strip().split()
            if toks:
                s = re.sub(r"[^a-z]", "", toks[-1].lower())
                if len(s) >= 2:
                    out.add(s)
        return out

    def _plausible(paper: dict, cand_title: str, cand_year, cand_surnames: set,
                   strict: bool = True) -> bool:
        """Is the candidate index record the same work as `paper`?

        strict=True (title search): require a near-exact title (>=0.85), or a
        strong title (>=0.75) with author-surname overlap. Year alone is NOT
        enough — generic titles in the same year collide too easily.
        strict=False (DOI already resolved): the DOI is strong evidence on its
        own, so only reject a DOI that clearly points to a different paper.
        """
        if not cand_title:
            return False
        sim = _title_similarity((paper.get("title") or "").lower(), cand_title.lower())
        ov = bool(_surnames(paper.get("authors", "")) & cand_surnames)
        # Short or generic titles collide easily, so require author corroboration
        # for them even at a high similarity score.
        ntoks = len(_normalize_title(paper.get("title") or "").split())
        if strict:
            if sim >= 0.85 and (ntoks >= 4 or ov):
                return True
            if sim >= 0.75 and ov:
                return True
            return False
        # DOI path: the DOI already resolved, so this only confirms the resolved
        # record is the same work (guards a real-but-wrong DOI). A shade more
        # lenient than the title path, but a weak title with only a same-year/
        # same-author coincidence is not enough to trust the DOI.
        if sim >= 0.80:
            return True
        if ov and sim >= 0.60:
            return True
        return False

    async def _doi_ok(session, paper: dict, doi_bare: str):
        """Confirm a DOI resolves AND points to THIS paper (guards against a
        fabricated DOI that happens to belong to a different real work). Returns
        (source|None, responded); responded means an index actually answered
        (HTTP 200/404), separating a real 'not found' from a network failure."""
        responded = False
        try:
            async with sem:
                async with session.get(
                    f"{OA_BASE}/works/https://doi.org/{urllib.parse.quote(doi_bare)}",
                    params={**_oa_auth_params(), "select": "title,publication_year,authorships"},
                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status in (200, 404):
                        responded = True
                    if r.status == 200:
                        w = await r.json()
                        sns = set()
                        for a in w.get("authorships", []) or []:
                            nm = (a.get("author", {}).get("display_name", "") or "").split()
                            if nm:
                                sns.add(re.sub(r"[^a-z]", "", nm[-1].lower()))
                        if _plausible(paper, w.get("title") or "", w.get("publication_year"), sns, strict=False):
                            return "openalex", True
        except Exception:
            pass
        try:
            async with sem:
                async with session.get(
                    f"{CR_BASE}/works/{urllib.parse.quote(doi_bare)}",
                    headers={"User-Agent": f"LitReviewPipeline/1.0 (mailto:{_OA_MAILTO})"},
                    timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status in (200, 404):
                        responded = True
                    if r.status == 200:
                        msg = (await r.json()).get("message", {})
                        titles = msg.get("title", [])
                        dp = (msg.get("issued", {}).get("date-parts", [[None]]) or [[None]])[0]
                        sns = {re.sub(r"[^a-z]", "", a.get("family", "").lower())
                               for a in (msg.get("author", []) or []) if a.get("family")}
                        if titles and _plausible(paper, titles[0], dp[0] if dp else None, sns, strict=False):
                            return "crossref", True
        except Exception:
            pass
        return None, responded

    async def _title_ok(session, paper: dict, title: str):
        """Title search across OpenAlex -> Crossref -> Semantic Scholar. Returns
        (source|None, responded); responded means at least one index answered 200."""
        responded = False
        try:
            async with sem:
                async with session.get(
                    f"{OA_BASE}/works",
                    params={**_oa_auth_params(), "filter": f'title.search:"{title}"',
                            "select": "title,publication_year,authorships", "per_page": "3"},
                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        responded = True
                        for w in (await r.json()).get("results", []):
                            sns = set()
                            for a in w.get("authorships", []) or []:
                                nm = (a.get("author", {}).get("display_name", "") or "").split()
                                if nm:
                                    sns.add(re.sub(r"[^a-z]", "", nm[-1].lower()))
                            if _plausible(paper, w.get("title") or "", w.get("publication_year"), sns, strict=True):
                                return "openalex", True
        except Exception:
            pass
        try:
            async with sem:
                async with session.get(
                    f"{CR_BASE}/works", params={"query.title": title, "rows": "3"},
                    headers={"User-Agent": f"LitReviewPipeline/1.0 (mailto:{_OA_MAILTO})"},
                    timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 200:
                        responded = True
                        for it in (await r.json()).get("message", {}).get("items", []):
                            titles = it.get("title", [])
                            dp = (it.get("issued", {}).get("date-parts", [[None]]) or [[None]])[0]
                            sns = {re.sub(r"[^a-z]", "", a.get("family", "").lower())
                                   for a in (it.get("author", []) or []) if a.get("family")}
                            if titles and _plausible(paper, titles[0], dp[0] if dp else None, sns, strict=True):
                                return "crossref", True
        except Exception:
            pass
        try:
            async with sem:
                async with session.get(
                    f"{S2_BASE}/paper/search",
                    params={"query": title, "fields": "title,year,authors", "limit": "3"},
                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        responded = True
                        for w in (await r.json()).get("data", []) or []:
                            sns = set()
                            for a in w.get("authors", []) or []:
                                nm = (a.get("name", "") or "").split()
                                if nm:
                                    sns.add(re.sub(r"[^a-z]", "", nm[-1].lower()))
                            if _plausible(paper, w.get("title") or "", w.get("year"), sns, strict=True):
                                return "s2", True
        except Exception:
            pass
        return None, responded

    async def _verify_one(session, paper: dict) -> str:
        paper.pop("_verify_error", None)
        title = (paper.get("title") or "").strip()
        if len(title) < 8:
            paper["verified"] = False
            paper["verified_source"] = ""
            return "unverified"
        doi_raw = (paper.get("doi") or "").strip()
        doi_bare = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi_raw) if doi_raw else ""
        responded = False
        doi_responded = False
        src, via = None, ""
        if doi_bare:
            src, resp = await _doi_ok(session, paper, doi_bare)
            doi_responded = resp
            responded = responded or resp
            if src:
                via = "doi"
        if not src:
            tsrc, resp = await _title_ok(session, paper, title)
            responded = responded or resp
            if tsrc:
                src, via = tsrc, "title"
        # Quarantine a DOI that an index answered for but did NOT confirm as this
        # paper (a real-but-wrong or fabricated DOI). Left in place, it would let
        # exact-DOI dedup merge this record into the DOI's actual, different work.
        # Only act when the DOI lookup actually responded, so a transient network
        # failure does not strip a possibly-correct DOI.
        if doi_bare and via != "doi" and doi_responded:
            paper["_unvalidated_doi"] = doi_raw
            paper["doi"] = ""
        if src:
            paper["verified"] = True
            paper["verified_source"] = src
            paper["verified_via"] = via
            return f"ok:{src}:{via}"
        paper["verified"] = False
        paper["verified_source"] = ""
        if not responded:
            # No index answered (network/outage): we could not check, so this is an
            # ERROR, not a confirmed miss. Step 3.5 keeps these rather than dropping.
            paper["_verify_error"] = True
            return "error"
        return "unverified"

    _log(f"Verifying {len(papers)} papers")
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[_verify_one(session, p) for p in papers], return_exceptions=True)
    for p, r in zip(papers, results):
        if isinstance(r, Exception):
            stats["errors"] += 1
            p["verified"] = False
            p["_verify_error"] = True
        elif r == "error":
            stats["errors"] += 1
        elif r == "unverified":
            stats["unverified"] += 1
        elif isinstance(r, str) and r.startswith("ok:"):
            _, s, via = r.split(":")
            stats["verified"] += 1
            stats[f"src_{s}"] = stats.get(f"src_{s}", 0) + 1
            stats["via_doi" if via == "doi" else "via_title"] += 1
    if log_path:
        try:
            log_path.write_text("\n".join(log_entries), encoding="utf-8")
        except Exception:
            pass
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Stage 5 — Merge and deduplicate lit review pipeline outputs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    input_group = parser.add_mutually_exclusive_group(required=False)
    input_group.add_argument("--inputs", nargs="+", help="Input JSON files")
    input_group.add_argument("--input-dir", help="Directory to glob *.json from")

    parser.add_argument("-o", "--output", default="merged_results.json",
                        help="Output JSON path (default: merged_results.json)")
    parser.add_argument("--model", default="deepseek-chat",
                        help="LLM model override (default: deepseek-chat)")
    parser.add_argument("--concurrency", type=int, default=100,
                        help="Max simultaneous LLM requests (default: 100)")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip Pass 2 (DOI-only dedup)")
    parser.add_argument("--verify", action=argparse.BooleanOptionalAction, default=True,
                        help="Cross-check every paper against OpenAlex/Crossref/Semantic Scholar "
                             "and DROP any that no index can confirm (anti-hallucination). On by "
                             "default; dropped papers are saved to <output>_unverified.json. "
                             "Use --no-verify to keep everything.")
    parser.add_argument("--yes", action="store_true",
                        help="Skip HITL confirmation checkpoint")
    parser.add_argument("--emit-pairs", metavar="PATH",
                        help="Agent-driven mode: run prepare + DOI dedup, write candidate pairs to "
                             "PATH, and exit before the LLM pass (no API call).")
    parser.add_argument("--ingest-verdicts", nargs=2, metavar=("EMIT", "VERDICTS"),
                        help="Agent-driven mode: read an --emit-pairs file plus the subagent verdicts "
                             "JSON, run the union-find merge, and write all outputs (no API call).")

    args = parser.parse_args()

    # Agent-driven ingest needs no input files — everything is in the emit file.
    if args.ingest_verdicts:
        _run_ingest(args)
        return
    if not (args.inputs or args.input_dir):
        parser.error("one of --inputs / --input-dir is required (or use --ingest-verdicts)")

    # Resolve input files
    if args.input_dir:
        input_dir = Path(args.input_dir)
        if not input_dir.is_dir():
            parser.error(f"Not a directory: {args.input_dir}")
        input_files = sorted(
            list(input_dir.glob("*.json")) + list(input_dir.glob("*.ris"))
        )
        if not input_files:
            parser.error(f"No .json or .ris files found in {args.input_dir}")
    else:
        input_files = [Path(f) for f in args.inputs]
        for f in input_files:
            if not f.exists():
                parser.error(f"File not found: {f}")

    # ── Load all papers ─────────────────────────────────────────────────
    print("=== Lit-Dedup (Stage 5) ===")
    print(f"Input files: {len(input_files)}")
    for f in input_files:
        print(f"  - {f}")

    all_papers: list[dict] = []
    for f in input_files:
        try:
            if f.suffix.lower() == ".ris":
                ris_text = f.read_text(encoding="utf-8")
                data = _parse_ris(ris_text)
                stem = f.stem
                for p in data:
                    p["_detected_source"] = _detect_source(p, stem)
                all_papers.extend(data)
                print(f"  Loaded {len(data)} papers from {f.name} (RIS)")
            else:
                data = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    print(f"  Warning: {f} is not a JSON array, skipping")
                    continue
                stem = f.stem
                for p in data:
                    p["_detected_source"] = _detect_source(p, stem)
                all_papers.extend(data)
                print(f"  Loaded {len(data)} papers from {f.name}")
        except Exception as e:
            print(f"  Error loading {f}: {e}")

    if not all_papers:
        # Zero papers is a valid outcome (e.g. upstream browser stages deferred
        # and the API search returned nothing). Write empty outputs and exit 0
        # so the pipeline run succeeds rather than failing.
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("[]", encoding="utf-8")
        ris_path = output_path.parent / f"{output_path.stem}.ris"
        ris_path.write_text("", encoding="utf-8")
        print(
            "No papers loaded from any input. "
            f"Wrote empty results to {output_path} and {ris_path}. Exiting 0."
        )
        sys.exit(0)

    total_input = len(all_papers)
    print(f"\nTotal input papers: {total_input}")

    # ── Step 1: Clean output schema ──────────────────────────────────────
    output_papers = []
    for p in all_papers:
        out = {
            "title": p.get("title", ""),
            "authors": p.get("authors", ""),
            "year": p.get("year", ""),
            "doi": p.get("doi", ""),
            "abstract": p.get("abstract", ""),
            "journal": p.get("journal", ""),
            "relevance": p.get("relevance", ""),
            "score": p.get("score", ""),
            "url": p.get("url", ""),
            "open_access": p.get("open_access", ""),
            "type": p.get("type", ""),
            "sources": p.get("sources", []),
            "alt_url": p.get("alt_url", ""),
        }
        for key in ("verified", "verified_source", "verified_via", "citations"):
            if key in p and key not in out:
                out[key] = p[key]
        # Carry the RIS-fallback detected source forward so downstream dedup
        # can aggregate it into `sources`.
        detected = p.get("_detected_source", "")
        if detected:
            out["_detected_source"] = detected
        src = p.get("source", "")
        if not src:
            src = detected
        if not src and out.get("sources"):
            src = out["sources"][0]
        out["source"] = src
        output_papers.append(out)

    # ── Step 2: Unified metadata enrichment ──────────────────────────────
    #
    # BEFORE dedup — fills authors, DOI, abstract, journal, year, URL
    # so that DOI dedup and LLM dedup have complete metadata to work with.
    #
    # Cascade per paper: OpenAlex → Crossref → S2 → GS snippet (abstract only)
    # Snippets (short GS text) are stashed and replaced only if a real abstract
    # is found; otherwise restored as last resort.
    #

    def _is_snippet(p: dict) -> bool:
        abstract = (p.get("abstract") or "").strip()
        if not abstract:
            return False
        if p.get("_abstract_source") == "google_scholar_snippet":
            return True
        if len(abstract) < 250 and ("\u2026" in abstract or "..." in abstract):
            return True
        return False

    def _needs_enrichment(p: dict) -> bool:
        if not (p.get("title") or "").strip():
            return False
        if not (p.get("authors") or "").strip():
            return True
        if not (p.get("doi") or "").strip():
            return True
        if not (p.get("abstract") or "").strip() or _is_snippet(p):
            return True
        if not (p.get("journal") or "").strip():
            return True
        if not (p.get("year") or "").strip():
            return True
        if not (p.get("url") or "").strip():
            return True
        return False

    to_enrich = [p for p in output_papers if _needs_enrichment(p)]

    snippet_stash: dict[int, str] = {}
    for p in to_enrich:
        if _is_snippet(p):
            snippet_stash[id(p)] = p["abstract"]
            p["abstract"] = ""

    if to_enrich:
        enrich_log_path = Path(args.output).parent / "metadata_enrichment.log"
        print(f"\n{len(to_enrich)}/{len(output_papers)} papers need metadata enrichment "
              f"(OpenAlex → Crossref → S2 → GS snippet) …")
        enrich_stats = asyncio.run(
            _enrich_all_metadata(to_enrich, log_path=enrich_log_path)
        )

        restored_snippets = 0
        upgraded_snippets = 0
        for p in to_enrich:
            pid = id(p)
            if pid in snippet_stash:
                if not (p.get("abstract") or "").strip():
                    p["abstract"] = snippet_stash[pid]
                    p["_abstract_source"] = "google_scholar_snippet"
                    restored_snippets += 1
                else:
                    p.pop("_abstract_source", None)
                    upgraded_snippets += 1

        for p in output_papers:
            if not (p.get("url") or "").strip() and (p.get("doi") or "").strip():
                p["url"] = p["doi"]

        print(f"  Results: {enrich_stats['enriched']}/{len(to_enrich)} papers improved")
        print(f"    Authors filled:   {enrich_stats['authors_filled']}")
        print(f"    DOIs filled:      {enrich_stats['dois_filled']}")
        print(f"    Abstracts filled: {enrich_stats['abstracts_filled']}")
        print(f"    Journals filled:  {enrich_stats['journals_filled']}")
        print(f"    Snippets → real:  {upgraded_snippets} (kept as snippet: {restored_snippets})")
        print(f"    Sources: OA={enrich_stats['src_openalex']} "
              f"CR={enrich_stats['src_crossref']} "
              f"S2={enrich_stats['src_s2']} "
              f"GS={enrich_stats['src_gs']}")
        print(f"  Log: {enrich_log_path}")

    # ── Step 3: Drop papers with no title ─────────────────────────────────
    # Keep any record that has a non-empty title. Author-less records are
    # legitimate (SearchAPI Google Scholar, NBER HTML fallback, transient
    # API outages), so missing authors alone is no longer grounds to drop.
    before_filter = len(output_papers)
    output_papers = [p for p in output_papers if (p.get("title") or "").strip()]
    dropped = before_filter - len(output_papers)
    if dropped:
        print(f"\nDropped {dropped} papers with no title")

    # ── Step 3.5: Verification (anti-hallucination cross-check) ───────────
    # Confirm each paper exists in OpenAlex / Crossref / Semantic Scholar and
    # drop any that none can confirm. Runs before DOI dedup so it covers both the
    # autonomous path and the agent-driven --emit-pairs path. Guarded so a total
    # miss (index outage) keeps everything rather than nuking the run.
    if getattr(args, "verify", True) and output_papers:
        print("\n--- Verification: cross-check vs OpenAlex / Crossref / Semantic Scholar ---")
        try:
            from collections import Counter
            vlog = Path(args.output).parent / "verification.log"
            total_v = len(output_papers)
            vstats = asyncio.run(_verify_all_papers(output_papers, log_path=vlog))
            verified = [p for p in output_papers if p.get("verified")]
            errored = [p for p in output_papers if not p.get("verified") and p.get("_verify_error")]
            unverified = [p for p in output_papers
                          if not p.get("verified") and not p.get("_verify_error")]
            print(f"  Confirmed {len(verified)}/{total_v} "
                  f"(via DOI={vstats.get('via_doi', 0)}, title={vstats.get('via_title', 0)}; "
                  f"OA={vstats.get('src_openalex', 0)} CR={vstats.get('src_crossref', 0)} "
                  f"S2={vstats.get('src_s2', 0)}); could-not-check={len(errored)}")
            # Degraded-run (outage) guard: keep everything ONLY when a large share
            # of papers could not be checked at all (an index outage). When the
            # indexes responded and simply confirmed nothing, that is a genuine
            # result, so the unconfirmed are dropped (anti-hallucination) rather
            # than keeping a corpus no index could vouch for.
            if len(errored) > 0.30 * total_v:
                print("  WARNING: verification looks degraded "
                      f"(confirmed={len(verified)}, could-not-check={len(errored)}); "
                      "keeping ALL papers rather than dropping.")
            elif unverified:
                unv_path = Path(args.output).parent / f"{Path(args.output).stem}_unverified.json"
                unv_path.parent.mkdir(parents=True, exist_ok=True)
                unv_path.write_text(json.dumps(unverified, indent=2, ensure_ascii=False),
                                    encoding="utf-8")
                bysrc = dict(Counter((p.get("source") or "unknown") for p in unverified))
                print(f"  Dropped {len(unverified)} unverified paper(s) by source {bysrc} "
                      f"→ {unv_path.name}")
                if errored:
                    print(f"  Kept {len(errored)} paper(s) that could not be checked (index errors).")
                output_papers = verified + errored
            else:
                print("  All papers confirmed.")
            for p in output_papers:
                p.pop("_verify_error", None)
        except Exception as e:
            print(f"  WARNING: verification failed ({type(e).__name__}: {e}); keeping all papers.")

    # ── Step 4: DOI dedup (now effective — papers have DOIs) ─────────────
    print("\n--- Pass 1: DOI dedup ---")
    after_doi, doi_merge_log = _doi_dedup(output_papers)
    doi_merged_count = len(output_papers) - len(after_doi)
    print(f"  After DOI dedup: {len(after_doi)} ({doi_merged_count} merged)")

    # ── Agent-driven emit: dump candidate pairs for the subagent to judge, then stop ──
    if args.emit_pairs:
        pairs = _build_candidate_pairs(after_doi)
        emit = {
            "papers": after_doi,
            "doi_merge_log": doi_merge_log,
            "meta": {
                "total_input": total_input,
                "before_filter": before_filter,
                "dropped": dropped,
                "after_doi": len(after_doi),
                "doi_merged_count": doi_merged_count,
            },
            "pairs": [
                {"i": i, "j": j,
                 "a": _format_paper_for_llm(after_doi[i]),
                 "b": _format_paper_for_llm(after_doi[j])}
                for i, j in pairs
            ],
        }
        emit_path = Path(args.emit_pairs)
        emit_path.parent.mkdir(parents=True, exist_ok=True)
        emit_path.write_text(json.dumps(emit, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[DEDUP] Emitted {len(pairs)} candidate pairs "
              f"({len(after_doi)} papers after DOI dedup) to {emit_path}")
        sys.exit(0)

    # ── Step 5: LLM fuzzy match (now has full metadata) ──────────────────
    llm_merge_log: list[dict[str, object]] = []
    llm_pairs_checked = 0
    after_llm = after_doi

    if not args.no_llm:
        if aiohttp is None:
            print("\n--- Pass 2: Skipped (aiohttp not installed; pip install aiohttp) ---")
            args.no_llm = True

    if not args.no_llm:
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

        if deepseek_key:
            print("\n--- Pass 2: LLM fuzzy match (DeepSeek) ---")
            after_llm, llm_merge_log, llm_pairs_checked = _llm_dedup(
                after_doi, deepseek_key, args.model, args.concurrency,
                use_anthropic=False,
            )
        elif anthropic_key:
            model = "claude-sonnet-4-6"
            print(f"\n--- Pass 2: LLM fuzzy match (Claude {model}) ---")
            print("  (DEEPSEEK_API_KEY not set, using ANTHROPIC_API_KEY fallback)")
            after_llm, llm_merge_log, llm_pairs_checked = _llm_dedup(
                after_doi, anthropic_key, model, args.concurrency,
                use_anthropic=True,
            )
        else:
            print("\n--- Pass 2: Skipped (no DEEPSEEK_API_KEY or ANTHROPIC_API_KEY) ---")
    else:
        print("\n--- Pass 2: Skipped (--no-llm) ---")

    llm_merged_count = len(after_doi) - len(after_llm)
    print(f"  After LLM dedup: {len(after_llm)} ({llm_merged_count} merged)")

    output_papers = after_llm

    # ── Summary ──────────────────────────────────────────────────────────
    print(f"\n=== Pipeline Summary ===")
    print(f"Total input papers:    {total_input}")
    print(f"After enrichment:      {before_filter} ({before_filter - dropped} with title)")
    print(f"After DOI dedup:       {len(after_doi)} ({doi_merged_count} merged)")
    if not args.no_llm:
        print(f"LLM pairs checked:     {llm_pairs_checked}")
    print(f"After LLM dedup:       {len(after_llm)} ({llm_merged_count} merged)")

    if not args.yes:
        try:
            answer = input("\nProceed with saving? [Y/n] ").strip().lower()
            if answer and answer != "y":
                print("Aborted.")
                sys.exit(0)
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)

    # ── Write outputs ───────────────────────────────────────────────────
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # JSON
    output_path.write_text(
        json.dumps(output_papers, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nSaved {len(output_papers)} papers to {output_path}")

    # RIS
    ris_path = output_path.parent / f"{output_path.stem}.ris"
    write_ris(output_papers, ris_path)
    print(f"RIS saved to {ris_path}")

    # Dedup log
    log_path = output_path.parent / "dedup_log.json"
    log_data = {
        "doi_merges": doi_merge_log,
        "llm_merges": llm_merge_log,
        "stats": {
            "total_input": total_input,
            "after_doi_dedup": len(after_doi),
            "after_llm_dedup": len(after_llm),
            "llm_pairs_checked": llm_pairs_checked,
        },
    }
    log_path.write_text(
        json.dumps(log_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Dedup log saved to {log_path}")

    # Markdown report
    report_path = output_path.parent / "dedup_report.md"
    write_dedup_report(llm_merge_log, doi_merge_log, log_data["stats"], report_path)
    print(f"Dedup report saved to {report_path}")

    # Source breakdown
    source_counts: dict[str, int] = {}
    for p in output_papers:
        for s in p.get("sources", []):
            source_counts[s] = source_counts.get(s, 0) + 1
    if source_counts:
        print("\nBy source (papers found by each):")
        for s, c in sorted(source_counts.items()):
            print(f"  {s}: {c}")


if __name__ == "__main__":
    main()
