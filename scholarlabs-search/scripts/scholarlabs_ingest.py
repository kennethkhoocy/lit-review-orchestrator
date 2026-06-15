#!/usr/bin/env python3
"""Scholar Labs ingest — parse scraped BibTeX into the pipeline schema.

This module is the UI-independent half of the Scholar Labs stage. The browser
driver (``scholarlabs_search.py``) collects one BibTeX entry per result from the
Google Scholar "Cite -> BibTeX" export and concatenates them into a single
``.bib`` file; this module parses that file, enriches each record with DOIs /
abstracts via Crossref + OpenAlex, and writes ``<stem>.json`` (+ a ``<stem>.bib``)
in the same schema the rest of the pipeline consumes.

It is imported by ``scholarlabs_search.py`` and can also be run standalone on any
BibTeX/RIS file:

    python scholarlabs_ingest.py --input refs.bib -o stage2_scholarlabs.json
    python scholarlabs_ingest.py --input refs.bib -o out.json --no-enrich

Enrichment uses the same two-tier title matching as the dedup stage: a free
normalized-string check, then an optional DeepSeek LLM check when
``DEEPSEEK_API_KEY`` is set. Scholar's BibTeX rarely carries a DOI, so the
Crossref title lookup is what resolves most identifiers here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# Load ~/.lit-review-pipeline.env if present (portable key store)
_env_file = Path.home() / ".lit-review-pipeline.env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file, override=False)
    except ImportError:
        pass

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

CROSSREF_API = "https://api.crossref.org/works"
OPENALEX_API = "https://api.openalex.org/works"
DEEPSEEK_API = "https://api.deepseek.com/chat/completions"
CONTACT_EMAIL = "litreview-bot@example.com"
REQUEST_TIMEOUT = 15


# ── BibTeX parsing (Scholar's "Cite -> BibTeX" export) ────────────────────────

def _detex(value: str) -> str:
    """Resolve the common TeX escapes Scholar's BibTeX leaves in field text."""
    value = re.sub(r"\\([&%_#${}])", r"\1", value)                     # \& -> &
    value = re.sub(r"\\[`'^\"~=.]\s*\{?([A-Za-z])\}?", r"\1", value)   # \'e -> e
    return value


def _strip_braces(value: str) -> str:
    value = value.strip().rstrip(",").strip()
    if value and value[0] in "{\"" and value[-1] in "}\"":
        value = value[1:-1]
    # Resolve TeX escapes, then drop stray braces and collapse whitespace.
    value = _detex(value)
    value = value.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", value).strip()


def parse_bibtex(text: str) -> list[dict]:
    """Parse BibTeX entries into paper dicts (tolerant of common formatting)."""
    papers = []
    # Split into @type{...} blocks by locating each entry start.
    for m in re.finditer(r"@\w+\s*\{", text):
        start = m.end()
        depth = 1
        i = start
        while i < len(text) and depth > 0:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        body = text[start:i - 1]

        # Field extraction: key = {value} | "value" | bareword , (allow nesting)
        fields: dict[str, str] = {}
        for fm in re.finditer(
            r'(\w+)\s*=\s*(\{(?:[^{}]|\{[^{}]*\})*\}|"[^"]*"|[^,\n]+)',
            body,
        ):
            key = fm.group(1).lower()
            fields[key] = _strip_braces(fm.group(2))

        title = fields.get("title", "")
        if not title:
            continue

        # BibTeX authors are "and"-separated; Scholar uses the "Last, First"
        # form, so flip each to "First Last" before joining with commas (which
        # the pipeline schema uses as the author separator).
        authors_raw = fields.get("author", "")
        names = []
        for a in re.split(r"\s+and\s+", authors_raw):
            a = a.strip()
            if not a:
                continue
            if "," in a:
                last, first = a.split(",", 1)
                a = f"{first.strip()} {last.strip()}".strip()
            names.append(a)
        authors = ", ".join(names)

        doi = fields.get("doi", "")
        if doi and not doi.startswith("http"):
            doi = f"https://doi.org/{doi}"

        # Scholar uses journal / booktitle / publisher depending on the source.
        journal = (fields.get("journal") or fields.get("booktitle")
                   or fields.get("publisher", ""))

        papers.append({
            "title": title,
            "authors": authors,
            "year": fields.get("year", ""),
            "journal": journal,
            "doi": doi,
            "abstract": fields.get("abstract", ""),
            "url": fields.get("url", doi),
        })
    return papers


# ── RIS parsing (fallback if a .ris file is fed standalone) ───────────────────

def parse_ris(text: str) -> list[dict]:
    """Parse standard multi-line RIS text into paper dicts."""
    papers: list[dict] = []
    current: dict[str, str] = {}
    authors: list[str] = []
    last_field: str | None = None

    def _flush() -> None:
        nonlocal current, authors, last_field
        if current.get("title"):
            if authors:
                current["authors"] = ", ".join(authors)
            doi = current.get("doi", "")
            if doi and not doi.startswith("http"):
                current["doi"] = f"https://doi.org/{doi}"
            current.setdefault("url", current.get("doi", ""))
            papers.append(current)
        current = {}
        authors = []
        last_field = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("ER  -"):
            _flush()
            continue
        if len(stripped) > 6 and stripped[2:6] == "  - ":
            tag = stripped[:2]
            value = stripped[6:].strip()
        else:
            if last_field == "authors" and authors:
                authors[-1] = f"{authors[-1]} {stripped}".strip()
            elif last_field:
                current[last_field] = f"{current.get(last_field, '')} {stripped}".strip()
            continue
        if tag in ("T1", "TI"):
            current["title"] = value; last_field = "title"
        elif tag == "AU":
            authors.append(value); last_field = "authors"
        elif tag in ("JO", "JF", "T2"):
            current["journal"] = value; last_field = "journal"
        elif tag == "PY":
            current["year"] = value; last_field = "year"
        elif tag == "DO":
            current["doi"] = value; last_field = "doi"
        elif tag in ("AB", "N2"):
            current["abstract"] = value; last_field = "abstract"
        elif tag == "UR":
            current["url"] = value; last_field = "url"
        else:
            last_field = None
    _flush()
    return papers


def parse_reference_file(path: Path) -> list[dict]:
    """Parse a BibTeX or RIS file, retrying the other parser if the first is empty.

    Scholar's export is BibTeX, but the fallback guards against a mislabelled or
    hand-supplied file silently producing an empty, "successful" result.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
    looks_bib = text.lstrip()[:1] == "@"
    if suffix == ".ris":
        primary, secondary = parse_ris, parse_bibtex
    elif suffix in (".bib", ".bibtex") or looks_bib:
        primary, secondary = parse_bibtex, parse_ris
    else:
        primary, secondary = parse_bibtex, parse_ris
    records = primary(text)
    if not records:
        records = secondary(text)
    return records


# ── Crossref / OpenAlex enrichment ────────────────────────────────────────────

_TITLE_MATCH_PROMPT = (
    "You are verifying whether two paper titles refer to the same academic work. "
    "Titles may differ in punctuation, capitalisation, subtitles, abbreviations, "
    "or minor wording (e.g. British vs American spelling). "
    "Reply with exactly YES or NO — nothing else."
)


def _normalize_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def _titles_match(query_title: str, candidate_title: str) -> bool:
    if not query_title or not candidate_title:
        return False
    if _normalize_title(query_title) == _normalize_title(candidate_title):
        return True
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return False
    try:
        resp = requests.post(
            DEEPSEEK_API,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": _TITLE_MATCH_PROMPT},
                    {"role": "user", "content": (
                        f"Title A: {query_title}\nTitle B: {candidate_title}"
                    )},
                ],
                "max_tokens": 4,
                "temperature": 0,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            answer = resp.json()["choices"][0]["message"]["content"].strip().upper()
            return answer.startswith("YES")
    except Exception:
        pass
    return False


def _crossref_title(data: dict) -> str:
    titles = data.get("title", [])
    return titles[0] if titles else ""


def _crossref_authors(data: dict) -> str:
    names = []
    for a in data.get("author", []):
        given, family = a.get("given", ""), a.get("family", "")
        if given and family:
            names.append(f"{given} {family}")
        elif family:
            names.append(family)
    return ", ".join(names)


def _crossref_year(data: dict) -> str:
    published = (data.get("published-print") or data.get("published-online")
                 or data.get("published") or data.get("created"))
    if published and "date-parts" in published:
        parts = published["date-parts"]
        if parts and parts[0] and parts[0][0]:
            return str(parts[0][0])
    return ""


def _crossref_journal(data: dict) -> str:
    names = data.get("container-title", [])
    return names[0] if names else ""


def _reconstruct_abstract(inverted_index: dict | None) -> str:
    if not inverted_index:
        return ""
    word_positions = []
    for word, positions in inverted_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(w for _, w in word_positions)


def _crossref_search_by_title(title: str, author_surname: str = "") -> dict | None:
    headers = {"User-Agent": f"LitReviewBot/1.0 (mailto:{CONTACT_EMAIL})"}
    params = {"query.bibliographic": title, "rows": 3}
    if author_surname:
        params["query.author"] = author_surname
    try:
        resp = requests.get(CROSSREF_API, params=params, headers=headers,
                            timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
    except Exception:
        return None
    for item in items:
        cr_title = _crossref_title(item)
        if not _titles_match(title, cr_title):
            continue
        doi = item.get("DOI", "")
        return {
            "doi": f"https://doi.org/{doi}" if doi else "",
            "title": cr_title,
            "authors": _crossref_authors(item),
            "year": _crossref_year(item),
            "journal": _crossref_journal(item),
            "type": item.get("type", ""),
        }
    return None


def _openalex_enrich(doi: str, title: str) -> dict | None:
    headers = {"User-Agent": f"LitReviewBot/1.0 (mailto:{CONTACT_EMAIL})"}
    work = None
    if doi:
        doi_url = doi if doi.startswith("http") else f"https://doi.org/{doi}"
        try:
            resp = requests.get(f"{OPENALEX_API}/{doi_url}", headers=headers,
                                timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                work = resp.json()
        except Exception:
            pass
    if not work and title:
        try:
            resp = requests.get(OPENALEX_API, headers=headers,
                                params={"filter": f'title.search:"{title}"',
                                        "per_page": 1},
                                timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                # Only trust the title-search hit if the titles actually match,
                # otherwise we would attach another work's DOI/abstract/journal.
                if results and _titles_match(
                        title, results[0].get("display_name") or results[0].get("title", "")):
                    work = results[0]
        except Exception:
            pass
    if not work:
        return None
    source = work.get("primary_location", {}).get("source") or {}
    doi_out = work.get("doi") or ""
    if doi_out and not doi_out.startswith("http"):
        doi_out = f"https://doi.org/{doi_out}"
    return {
        "abstract": _reconstruct_abstract(work.get("abstract_inverted_index")),
        "journal": source.get("display_name") or "",
        "citations": work.get("cited_by_count", 0),
        "open_access": work.get("open_access", {}).get("is_oa", False),
        "doi": doi_out,
    }


def enrich_papers(papers: list[dict], verbose: bool = True) -> list[dict]:
    """Fill missing DOIs (Crossref) and abstracts/journals (OpenAlex)."""
    if verbose and not os.environ.get("DEEPSEEK_API_KEY"):
        print("  WARNING: DEEPSEEK_API_KEY not set. Fuzzy title matching disabled; "
              "only exact normalised matches resolve missing DOIs.")
    doi_added = abstract_added = 0
    total = len(papers)
    for i, paper in enumerate(papers):
        title = paper.get("title", "").strip()
        if not title:
            continue
        if not paper.get("doi"):
            authors = paper.get("authors", "")
            surname = authors.split(",")[0].strip().split()[-1] if authors.strip() else ""
            try:
                match = _crossref_search_by_title(title, surname)
            except Exception:
                match = None
            if match and match.get("doi"):
                paper["doi"] = match["doi"]
                paper["journal"] = match.get("journal") or paper.get("journal", "")
                paper["verified"] = True
                doi_added += 1
        try:
            oa = _openalex_enrich(paper.get("doi", ""), title)
        except Exception:
            oa = None
        if oa:
            if not paper.get("abstract") and oa.get("abstract"):
                paper["abstract"] = oa["abstract"]
                abstract_added += 1
            if not paper.get("journal") and oa.get("journal"):
                paper["journal"] = oa["journal"]
            paper.setdefault("citations", oa.get("citations", 0))
            paper.setdefault("open_access", oa.get("open_access", False))
            if not paper.get("doi") and oa.get("doi"):
                paper["doi"] = oa["doi"]
                doi_added += 1
        if verbose and ((i + 1) % 10 == 0 or (i + 1) == total):
            print(f"  Enrichment progress: {i + 1}/{total}")
        time.sleep(0.3)
    if verbose:
        print(f"  Enrichment complete: +{doi_added} DOIs, +{abstract_added} abstracts")
    return papers


# ── Normalize + emit ──────────────────────────────────────────────────────────

def normalize_results(papers: list[dict], source: str = "scholarlabs") -> list[dict]:
    out = []
    for p in papers:
        out.append({
            "title": p.get("title", ""),
            "authors": p.get("authors", ""),
            "year": p.get("year", ""),
            "doi": p.get("doi", ""),
            "abstract": p.get("abstract", ""),
            "journal": p.get("journal", ""),
            "relevance": p.get("relevance", ""),
            "score": p.get("score", ""),
            "url": p.get("url", ""),
            "source": p.get("source", source),
            "verified": p.get("verified", False),
            "citations": p.get("citations", 0),
            "open_access": p.get("open_access", False),
            "type": p.get("type", ""),
        })
    return out


def to_ris(papers: list[dict]) -> str:
    """Emit a normalized multi-line RIS file from paper dicts."""
    lines = []
    for p in papers:
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
    return "\n".join(lines)


def _bibtex_key(p: dict, i: int) -> str:
    authors = p.get("authors") or ""
    first = authors.split(",")[0].strip().split()[-1] if authors.strip() else "ref"
    stem = re.sub(r"[^A-Za-z0-9]", "", f"{first}{p.get('year', '')}") or "ref"
    return f"{stem}{i}"


def to_bibtex(papers: list[dict]) -> str:
    """Emit a normalized BibTeX file from paper dicts."""
    out = []
    for i, p in enumerate(papers):
        authors = p.get("authors") or ""
        bib_authors = " and ".join(a.strip() for a in authors.split(",") if a.strip())
        fields = []
        if bib_authors:
            fields.append(f"  author = {{{bib_authors}}}")
        if p.get("title"):
            fields.append(f"  title = {{{p['title']}}}")
        if p.get("journal"):
            fields.append(f"  journal = {{{p['journal']}}}")
        if p.get("year"):
            fields.append(f"  year = {{{p['year']}}}")
        doi = (p.get("doi") or "").replace("https://doi.org/", "")
        if doi:
            fields.append(f"  doi = {{{doi}}}")
        if p.get("abstract"):
            fields.append(f"  abstract = {{{p['abstract']}}}")
        if p.get("url"):
            fields.append(f"  url = {{{p['url']}}}")
        out.append(f"@article{{{_bibtex_key(p, i)},\n" + ",\n".join(fields) + "\n}")
    return "\n\n".join(out) + "\n"


def ingest(input_path: Path, output_path: Path, *, enrich: bool = True,
           source: str = "scholarlabs", sibling: str = "bibtex",
           relevance: list[str] | None = None, verbose: bool = True) -> list[dict]:
    """Parse -> (optionally enrich) -> normalize -> write JSON + a sibling file.

    ``relevance`` is an optional list of per-result one-line AI summaries (in the
    same order Scholar Labs returned them); when present and the count matches,
    each is attached to its record's ``relevance`` field.
    """
    raw = parse_reference_file(input_path)
    if verbose:
        print(f"[INGEST] Parsed {len(raw)} records from {input_path.name}")
    if relevance and len(relevance) == len(raw):
        for rec, note in zip(raw, relevance):
            if note:
                rec["relevance"] = note
    if enrich and raw:
        if verbose:
            print(f"[INGEST] Enriching {len(raw)} records via Crossref + OpenAlex...")
        raw = enrich_papers(raw, verbose=verbose)
    results = normalize_results(raw, source=source)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    sibling_path = None
    if sibling == "bibtex":
        sibling_path = output_path.with_suffix(".bib")
        sibling_path.write_text(to_bibtex(results), encoding="utf-8")
    elif sibling == "ris":
        sibling_path = output_path.with_suffix(".ris")
        sibling_path.write_text(to_ris(results), encoding="utf-8")
    if verbose:
        with_doi = sum(1 for p in results if p.get("doi"))
        with_abs = sum(1 for p in results if p.get("abstract"))
        print(f"[INGEST] Wrote {output_path} ({len(results)} papers; "
              f"{with_doi} DOIs, {with_abs} abstracts)")
        if sibling_path:
            print(f"[INGEST] Wrote {sibling_path}")
    return results


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Parse a Scholar Labs BibTeX/RIS export into the pipeline schema.")
    ap.add_argument("--input", "-i", required=True,
                    help="Path to the exported .bib or .ris file")
    ap.add_argument("--output", "-o", default="stage2_scholarlabs.json",
                    help="Output JSON path (a sibling .bib is also written)")
    ap.add_argument("--source", default="scholarlabs", help="source tag for records")
    ap.add_argument("--sibling", choices=("bibtex", "ris", "none"), default="bibtex",
                    help="Reference-manager copy to write beside the JSON (default: bibtex)")
    ap.add_argument("--no-enrich", action="store_true",
                    help="Skip Crossref/OpenAlex enrichment (parse only)")
    args = ap.parse_args()

    input_path = Path(args.input)
    if not input_path.is_file():
        sys.exit(f"Error: input file not found: {input_path}")

    ingest(input_path, Path(args.output), enrich=not args.no_enrich,
           source=args.source, sibling=args.sibling)


if __name__ == "__main__":
    main()
