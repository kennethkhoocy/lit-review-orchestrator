#!/usr/bin/env python3
"""Stage 2b ingest — parse a Gemini Deep Research report into pipeline records.

Primary signal: the "KEY PAPERS" section the driver asks the agent to emit, one
paper per line as `Title | Authors | Year | Venue | DOI-or-URL`. Fallback: when
that yields very little, any (title, url) citations found in the raw Interactions
response. Records are written in the pipeline schema with source="deepresearch";
metadata is lightly enriched via Crossref (best-effort) and further enriched
downstream at the dedup stage.

Importable as ingest_report(); also runnable standalone on a saved report .md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.I)


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").lower().strip())


def _title_sim(a: str, b: str) -> float:
    wa, wb = set(_norm_title(a).split()), set(_norm_title(b).split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _split_doi_or_url(s: str) -> tuple[str, str]:
    """Return (doi_url, url) from the trailing 'DOI or URL' field."""
    s = (s or "").strip()
    if not s:
        return "", ""
    m = DOI_RE.search(s)
    if m:
        return f"https://doi.org/{m.group(0).rstrip(').,;')}", ""
    if s.startswith("http"):
        return "", s
    return "", ""


def _parse_pipe_rows(section_lines: list[str]) -> list[dict]:
    """Parse pipe-delimited rows: Title | Authors | Year | Venue | DOI-or-URL."""
    records: list[dict] = []
    for ln in section_lines:
        if ln.count("|") < 3:
            continue
        parts = [p.strip() for p in ln.split("|")]
        # Markdown tables wrap each row in pipes ("| a | b |"), producing empty
        # cells at both ends; drop them so the title is parts[0], not "".
        while parts and parts[0] == "":
            parts.pop(0)
        while parts and parts[-1] == "":
            parts.pop()
        if len(parts) < 4:
            continue
        # Skip Markdown separator rows like |---|:--:|---|.
        if all(re.fullmatch(r":?-{2,}:?", p or "") for p in parts):
            continue
        # Strip leading bullets/emphasis and an enumerator ("1. "/"1) ") only --
        # NOT all leading digits, so a title like "1984 and Markets" survives.
        title = re.sub(r"^[\-\*\s]+", "", parts[0])
        title = re.sub(r"^\d+\s*[.\)]\s+", "", title)
        title = title.strip().strip("*_").strip()
        if not title or title.lower() == "title":
            continue
        authors_raw = parts[1] if len(parts) > 1 else ""
        if authors_raw.lower() in ("authors", "author", "authors (comma-separated)"):
            authors_raw = ""
        authors, author_year = _clean_authors(authors_raw)
        year = ""
        ym = re.search(r"(19|20)\d{2}", parts[2] if len(parts) > 2 else "")
        if ym:
            year = ym.group(0)
        if not year:
            year = author_year
        journal = parts[3] if len(parts) > 3 else ""
        if journal.lower() in ("venue", "journal", "venue or journal", "venue/journal"):
            journal = ""
        doi, url = _split_doi_or_url(parts[4] if len(parts) > 4 else "")
        records.append({"title": title, "authors": authors, "year": year,
                        "journal": journal, "doi": doi, "url": url})
    return records


def _clean_authors(a: str) -> tuple[str, str]:
    """From an author string that may end in '(YEAR)', return (authors, year)."""
    a = (a or "").strip().strip("*_").strip()
    a = re.sub(r"\s*\[cite[:\s][^\]]*\]", "", a, flags=re.I)   # drop [cite: ..] noise
    year = ""
    ym = re.search(r"(19|20)\d{2}", a)
    if ym:
        year = ym.group(0)
    a = re.sub(r"\s*\((?:[^()]*)\)\s*$", "", a).strip()        # strip trailing (YEAR)
    a = re.sub(r",?\s+and\s+", ", ", a)                         # "X and Y" -> "X, Y"
    a = re.sub(r"\s*,\s*,\s*", ", ", a).strip().strip(",").strip()
    return a, year


def _parse_narrative_entries(section_lines: list[str]) -> list[dict]:
    """Parse the narrative KEY PAPERS layout Deep Research often emits instead of
    a pipe table:

        ### 1. *Title of the Paper*
        **Authors:** First Author and Second Author (2021)
        **Venue:** Journal Name            (optional)
        ...prose...

    The title comes from each H3+ heading; authors/year/venue come from the bold
    labels in the block that follows, up to the next heading.
    """
    heads = [i for i, ln in enumerate(section_lines)
             if re.match(r"^\s{0,3}#{3,6}\s+\S", ln)]
    records: list[dict] = []
    for k, hi in enumerate(heads):
        raw_title = re.sub(r"^\s{0,3}#{3,6}\s+", "", section_lines[hi]).strip()
        numbered = bool(re.match(r"^\s*\d+\s*[.\):\-]\s+\S", raw_title))
        title = re.sub(r"^\s*\d+\s*[.\):\-]\s*", "", raw_title)   # drop "1." numbering
        title = re.sub(r"\s*\[cite[:\s][^\]]*\]", "", title, flags=re.I)
        title = title.strip().strip("*_").strip()
        if not title or title.lower() == "title":
            continue
        block_end = heads[k + 1] if k + 1 < len(heads) else len(section_lines)
        authors, year, journal = "", "", ""
        for ln in section_lines[hi + 1:block_end]:
            m = re.match(r"^\s*[*_]*\s*authors?\s*[*_]*\s*[:\-]\s*(.+)$", ln, re.I)
            if m and not authors:
                authors, y = _clean_authors(m.group(1))
                year = year or y
                continue
            mv = re.match(r"^\s*[*_]*\s*(?:venue|journal|publication|outlet)\s*[*_]*\s*[:\-]\s*(.+)$", ln, re.I)
            if mv and not journal:
                journal = mv.group(1).strip().strip("*_").strip()
                continue
            if not year:
                my = re.search(r"\((19|20)\d{2}\)", ln)
                if my:
                    year = my.group(0).strip("()")
        # Emit only plausible paper entries: a numbered heading (### 1. ...) or a
        # heading followed by an Authors line. Drops stray notes such as
        # "### Methodological note" that can appear inside the section.
        if not (authors or numbered):
            continue
        records.append({"title": title, "authors": authors, "year": year,
                        "journal": journal, "doi": "", "url": ""})
    return records


def _merge_records(primary: list[dict], secondary: list[dict]) -> list[dict]:
    """Merge two record lists, dedup by normalized title, filling empty fields.

    `primary` order is preserved and its non-empty values win; `secondary` only
    fills gaps. Used so a section mixing pipe rows and narrative entries keeps
    every paper instead of discarding one layout.
    """
    out: list[dict] = []
    by_title: dict[str, dict] = {}
    for rec in list(primary) + list(secondary):
        k = _norm_title(rec.get("title", ""))
        if not k:
            continue
        if k in by_title:
            tgt = by_title[k]
            for f in ("authors", "year", "journal", "doi", "url"):
                if not tgt.get(f) and rec.get(f):
                    tgt[f] = rec[f]
        else:
            nr = dict(rec)
            by_title[k] = nr
            out.append(nr)
    return out


def parse_key_papers(report_text: str) -> list[dict]:
    """Parse the 'KEY PAPERS' section into records.

    Anchored strictly on a KEY PAPERS heading so arbitrary tables in the synthesis
    prose are never mistaken for papers. Within that section we accept BOTH the
    requested pipe-delimited rows AND the narrative '### N. Title / **Authors:**'
    layout Deep Research frequently emits, and merge the two so a mixed section
    loses nothing. Headings inside fenced code blocks are ignored.
    """
    if not report_text:
        return []
    lines = report_text.splitlines()
    start = None
    in_fence = False
    for i, ln in enumerate(lines):
        if re.match(r"^\s*(```|~~~)", ln):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if re.match(r"^\s*[#*>\-\s]*key papers\s*[:*]*\s*$", ln, re.I):
            start = i + 1
            break
    if start is None:
        # No KEY PAPERS section present: do NOT scrape arbitrary pipe tables from
        # the synthesis prose — taxonomy/method tables would be mis-read as papers.
        # Let the caller fall back to citation extraction instead.
        return []
    # Bound the section at the next top-level (H1/H2) heading; narrative paper
    # entries are H3+, so they must not be treated as the section's end. Headings
    # inside fenced code blocks do not count.
    end = len(lines)
    in_fence = False
    for j in range(start, len(lines)):
        if re.match(r"^\s*(```|~~~)", lines[j]):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if re.match(r"^\s{0,3}#{1,2}\s+\S", lines[j]):
            end = j
            break
    section = lines[start:end]
    return _merge_records(_parse_pipe_rows(section), _parse_narrative_entries(section))


def _extract_citations(raw) -> list[dict]:
    """Defensive: collect (title, url) pairs from anywhere in the raw response."""
    out: list[dict] = []

    seen: set[int] = set()

    def walk(o, depth=0):
        if depth > 200 or id(o) in seen:
            return
        if isinstance(o, dict):
            seen.add(id(o))
            url = o.get("url") or o.get("uri") or ""
            title = o.get("title") or o.get("name") or ""
            if (isinstance(url, str) and url.startswith("http")
                    and isinstance(title, str) and len(title.strip()) > 8):
                out.append({"title": title.strip(), "authors": "", "year": "",
                            "journal": "", "doi": "", "url": url.strip()})
            for v in o.values():
                walk(v, depth + 1)
        elif isinstance(o, list):
            seen.add(id(o))
            for v in o:
                walk(v, depth + 1)

    walk(raw or {})
    return out


def _dedup_by_title(records: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for r in records:
        k = _norm_title(r.get("title", ""))
        if k and k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _crossref_fill(rec: dict, mailto: str = "lit-review-pipeline@example.com") -> dict:
    """Best-effort: fill DOI/authors/year/journal from Crossref by title."""
    import requests
    title = rec.get("title", "")
    if not title or rec.get("doi"):
        return rec
    try:
        r = requests.get(
            "https://api.crossref.org/works",
            params={"query.bibliographic": title, "rows": 1, "mailto": mailto},
            timeout=20,
        )
        if not r.ok:
            return rec
        items = r.json().get("message", {}).get("items", [])
        if not items:
            return rec
        it = items[0]
        if _title_sim(title, " ".join(it.get("title") or [])) < 0.5:
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


def _to_schema(rec: dict) -> dict:
    return {
        "title": rec.get("title", ""), "authors": rec.get("authors", ""),
        "year": rec.get("year", ""), "doi": rec.get("doi", ""),
        "abstract": "", "journal": rec.get("journal", ""),
        "url": rec.get("url", ""), "source": "deepresearch",
        "verified": False, "citations": 0, "open_access": False,
    }


def _write_bib(records: list[dict], path: Path) -> None:
    entries: list[str] = []
    keycount: dict[str, int] = {}
    for p in records:
        authors = (p.get("authors") or "").strip()
        year = str(p.get("year") or "").strip()
        surname = "Unknown"
        if authors:
            toks = authors.split(",")[0].strip().split()
            if toks:
                surname = re.sub(r"[^A-Za-z]", "", toks[-1]) or "Unknown"
        base = f"{surname}{year}"
        c = keycount.get(base, 0)
        keycount[base] = c + 1
        key = base if c == 0 else f"{base}{chr(97 + c)}"
        lines = [f"@article{{{key},"]
        if p.get("title"):
            lines.append(f"  title = {{{p['title']}}},")
        if authors:
            lines.append(f"  author = {{{' and '.join(a.strip() for a in authors.split(',') if a.strip())}}},")
        if year:
            lines.append(f"  year = {{{year}}},")
        if p.get("journal"):
            lines.append(f"  journal = {{{p['journal']}}},")
        doi = (p.get("doi") or "").replace("https://doi.org/", "")
        if doi:
            lines.append(f"  doi = {{{doi}}},")
        if p.get("url"):
            lines.append(f"  url = {{{p['url']}}},")
        lines.append("}")
        entries.append("\n".join(lines))
    path.write_text("\n\n".join(entries) + ("\n" if entries else ""), encoding="utf-8")


def ingest_report(report_text: str, raw_response, output, enrich: bool = True) -> list[dict]:
    output = Path(output)
    records = parse_key_papers(report_text)
    # Fallback to raw-response citations ONLY when nothing parsed from the anchored
    # KEY PAPERS section, so a legitimate small (3-4 paper) section is not polluted
    # with arbitrary grounding-URL citations drawn from elsewhere in the response.
    if not records:
        seen = {_norm_title(r["title"]) for r in records}
        for c in _extract_citations(raw_response):
            if _norm_title(c["title"]) not in seen:
                seen.add(_norm_title(c["title"]))
                records.append(c)
    records = _dedup_by_title(records)
    if enrich:
        for r in records[:60]:
            _crossref_fill(r)
    schema = [_to_schema(r) for r in records]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_bib(schema, output.with_suffix(".bib"))
    return schema


def main() -> None:
    ap = argparse.ArgumentParser(description="Parse a saved Gemini Deep Research report into pipeline JSON.")
    ap.add_argument("--report", required=True, help="Saved Deep Research report .md")
    ap.add_argument("--raw", default="", help="Optional saved raw response .json")
    ap.add_argument("-o", "--output", default="stage2b_deepresearch.json", help="Output JSON path")
    ap.add_argument("--no-enrich", action="store_true", help="Skip Crossref enrichment")
    args = ap.parse_args()
    report = Path(args.report).read_text(encoding="utf-8")
    raw = {}
    if args.raw and Path(args.raw).is_file():
        raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    res = ingest_report(report, raw, Path(args.output), enrich=not args.no_enrich)
    print(f"Wrote {len(res)} papers to {args.output}")


if __name__ == "__main__":
    main()
