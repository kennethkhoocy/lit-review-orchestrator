#!/usr/bin/env python3
"""Manuscript parser — extract clean text and structure from .docx and .tex.

Bundled with lit-review-orchestrator so the pipeline works from a fresh clone.
This is the front-end of the pipeline: it turns a Word or LaTeX manuscript
(or an abstract / proposal) into a structured dict that the extraction stage
feeds to Claude.

Dependencies:
    .docx  -> python-docx; the body is walked in document order, descending
              through revision (w:ins) and content-control (w:sdt) wrappers so
              no block is lost, and tracked-change text (w:t + w:delText) is
              included. Footnotes/endnotes are read straight from the .docx zip.
              defusedxml is used for the note XML when available.
    .tex   -> a pragmatic de-TeX implemented here (no external dependency).

Security: both formats may be untrusted. `\\input`/`\\include` targets are
contained to the manuscript's own directory tree (no absolute paths or `..`
escapes) with depth/file-count/size caps, and .docx archives are size-checked
before parsing to bound zip-bomb / XML-expansion exposure.

Returned schema (parse_manuscript):
    {"format", "path", "title", "abstract",
     "sections": [{"heading", "text"}], "footnotes": [...], "n_chars"}

Run standalone to inspect a parse:
    python manuscript_parser.py paper.tex --json
    python manuscript_parser.py paper.docx
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

# Prefer defusedxml for untrusted note XML; fall back to stdlib ElementTree.
try:
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except Exception:  # pragma: no cover - defusedxml is an optional hardening dep
    _xml_fromstring = ET.fromstring

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

# OOXML wordprocessing namespace
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Untrusted-input safety limits
_MAX_DOCX_UNCOMPRESSED = 400 * 1024 * 1024   # total uncompressed bytes in a .docx
_MAX_DOCX_MEMBER = 100 * 1024 * 1024         # any single member
_MAX_NOTE_XML = 60 * 1024 * 1024             # footnotes/endnotes member
_MAX_DOCX_MEMBERS = 5000                     # zip entry count (many-tiny-files bomb)
_MAX_DOCX_RATIO = 200                        # max per-member uncompressed/compressed ratio
_MAX_TEX_INCLUDE_FILES = 60
_MAX_TEX_INCLUDE_BYTES = 8 * 1024 * 1024     # per included file
_MAX_TEX_TOTAL_BYTES = 32 * 1024 * 1024      # total inlined across all includes
_MAX_TEX_ROOT_BYTES = 32 * 1024 * 1024       # cap on the root .tex file itself
_MAX_TEX_DEPTH = 6


# ===========================================================================
# Public entry point
# ===========================================================================

def parse_manuscript(path: str | Path) -> dict:
    """Parse a .docx or .tex manuscript into a structured dict."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Manuscript not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".docx":
        parsed = _parse_docx(path)
        parsed["format"] = "docx"
    elif suffix in (".tex", ".latex"):
        parsed = _parse_tex(path)
        parsed["format"] = "tex"
    elif suffix == ".doc":
        raise ValueError(
            ".doc (legacy Word) is not supported. Convert to .docx first "
            "(e.g. open in Word and Save As .docx)."
        )
    else:
        raise ValueError(
            f"Unsupported manuscript type '{suffix}'. Use .docx or .tex."
        )

    parsed["path"] = str(path)
    parsed["n_chars"] = len(build_salient_text(parsed))
    return parsed


# ===========================================================================
# DOCX
# ===========================================================================

def _check_docx_zip(path: Path) -> None:
    """Reject corrupt or zip-bomb archives before handing them to python-docx."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > _MAX_DOCX_MEMBERS:
                raise ValueError(
                    f"{path.name}: {len(infos)} zip members exceeds the "
                    f"{_MAX_DOCX_MEMBERS} limit — refusing to parse."
                )
            total = 0
            for info in infos:
                if info.file_size > _MAX_DOCX_MEMBER:
                    raise ValueError(
                        f"{path.name}: member '{info.filename}' is too large "
                        f"({info.file_size} bytes) — refusing to parse."
                    )
                # Compression-ratio guard: a small compressed member that
                # explodes to a large uncompressed size is a zip-bomb signature.
                if (info.compress_size > 0
                        and info.file_size > 1024 * 1024
                        and info.file_size / info.compress_size > _MAX_DOCX_RATIO):
                    raise ValueError(
                        f"{path.name}: member '{info.filename}' has a suspicious "
                        f"compression ratio — refusing to parse."
                    )
                total += info.file_size
            if total > _MAX_DOCX_UNCOMPRESSED:
                raise ValueError(
                    f"{path.name}: uncompressed size {total} bytes exceeds the "
                    f"{_MAX_DOCX_UNCOMPRESSED}-byte limit — refusing to parse."
                )
    except zipfile.BadZipFile:
        raise ValueError(f"{path.name}: not a valid .docx (bad zip).")


def _iter_block_items(body, qn):
    """Yield ('p', element) / ('tbl', element) in document order.

    Descends through revision wrappers (``w:ins``) and content controls
    (``w:sdt`` / ``w:sdtContent``) so paragraphs nested in them are not lost,
    but never descends into a ``w:tbl`` (tables are emitted whole, which keeps
    their inner paragraphs from being double-counted).
    """
    P, TBL = qn("w:p"), qn("w:tbl")
    containers = {qn("w:sdt"), qn("w:sdtContent"), qn("w:ins")}

    def walk(parent):
        for ch in parent:
            tag = ch.tag
            if tag == P:
                yield "p", ch
            elif tag == TBL:
                yield "tbl", ch
            elif tag in containers:
                yield from walk(ch)

    yield from walk(body)


def _para_style_id(p_el, qn) -> str:
    """Read a paragraph's style id straight from the XML (no doc.paragraphs)."""
    pPr = p_el.find(qn("w:pPr"))
    if pPr is None:
        return ""
    pStyle = pPr.find(qn("w:pStyle"))
    if pStyle is None:
        return ""
    return pStyle.get(qn("w:val")) or ""


def _parse_docx(path: Path) -> dict:
    from docx import Document  # imported lazily so .tex parsing needs no dep
    from docx.oxml.ns import qn
    from docx.table import Table

    _check_docx_zip(path)
    doc = Document(str(path))

    title = ""
    abstract_parts: list[str] = []
    sections: list[dict] = []
    cur = {"heading": "(body)", "text": []}  # type: ignore[var-annotated]
    in_abstract = False

    def _flush():
        if cur["text"]:
            sections.append(
                {"heading": cur["heading"], "text": "\n".join(cur["text"]).strip()}
            )

    for kind, el in _iter_block_items(doc.element.body, qn):
        if kind == "p":
            txt = _element_text(el)
            if not txt:
                continue
            style = _para_style_id(el, qn).lower()

            if style == "title" and not title:
                title = txt
                continue

            if style.startswith("heading") or style == "subtitle":
                _flush()
                cur = {"heading": txt, "text": []}
                in_abstract = txt.lower().strip().rstrip(":") == "abstract"
                continue

            if style == "abstract" or in_abstract:
                abstract_parts.append(txt)
                continue

            cur["text"].append(txt)
        else:  # table
            try:
                ttext = _docx_table_text(Table(el, doc))
            except Exception:
                ttext = ""
            if ttext:
                cur["text"].append(ttext)

    _flush()

    # Title fallback: first short standalone line before any real heading.
    if not title:
        for s in sections:
            if s["heading"] == "(body)" and s["text"]:
                first = s["text"].split("\n", 1)[0].strip()
                if 0 < len(first) <= 250:
                    title = first
                break

    footnotes = _docx_notes(path, "footnotes") + _docx_notes(path, "endnotes")

    return {
        "title": title,
        "abstract": "\n".join(abstract_parts).strip(),
        "sections": [s for s in sections if s["text"]],
        "footnotes": footnotes,
    }


def _element_text(element) -> str:
    """All text within an element, including tracked-change ins/del runs.

    Walks ``w:t`` and ``w:delText`` (so inserted *and* deleted text is kept) and
    renders ``w:tab`` / ``w:br`` / ``w:cr`` as separators so words are not glued
    together across breaks. ``para.text`` would omit ``w:ins``/``w:del`` content.
    """
    from docx.oxml.ns import qn

    wt, wdel = qn("w:t"), qn("w:delText")
    wtab, wbr, wcr = qn("w:tab"), qn("w:br"), qn("w:cr")
    parts: list[str] = []
    for n in element.iter():
        if n.tag in (wt, wdel):
            if n.text:
                parts.append(n.text)
        elif n.tag == wtab:
            parts.append("\t")
        elif n.tag in (wbr, wcr):
            parts.append("\n")
    return "".join(parts).strip()


def _docx_table_text(tbl, max_rows: int = 1000) -> str:
    """Text rendering of a table (cell text, row by row).

    The row cap is a safety bound for pathological inputs only; ordinary tables
    are preserved in full and the final size is bounded by ``build_salient_text``.
    """
    lines = []
    for r, row in enumerate(tbl.rows):
        if r >= max_rows:
            break
        cells = [" ".join(_element_text(c._element).split()) for c in row.cells]
        line = " | ".join(c for c in cells if c)
        if line.strip(" |"):
            lines.append(line)
    return "\n".join(lines).strip()


def _docx_notes(path: Path, which: str) -> list[str]:
    """Read footnotes.xml / endnotes.xml from the .docx zip, in document order."""
    member = f"word/{which}.xml"
    tag = which[:-1]  # "footnotes" -> "footnote"
    try:
        with zipfile.ZipFile(path) as zf:
            if member not in zf.namelist():
                return []
            if zf.getinfo(member).file_size > _MAX_NOTE_XML:
                return []
            xml = zf.read(member)
    except Exception:
        return []

    try:
        root = _xml_fromstring(xml)
    except Exception:
        return []

    wt, wdel = f"{_W}t", f"{_W}delText"
    notes: list[str] = []
    for note in root.findall(f"{_W}{tag}"):
        if note.get(f"{_W}type") in ("separator", "continuationSeparator"):
            continue
        text = "".join(
            n.text for n in note.iter() if n.tag in (wt, wdel) and n.text
        ).strip()
        if text:
            notes.append(text)
    return notes


# ===========================================================================
# LaTeX
# ===========================================================================

def _read_text_tolerant(path: Path) -> str:
    """Read a .tex file as UTF-8 (Latin-1 fallback), size-capped for untrusted input."""
    with open(path, "rb") as f:
        data = f.read(_MAX_TEX_ROOT_BYTES + 1)
    if len(data) > _MAX_TEX_ROOT_BYTES:
        data = data[:_MAX_TEX_ROOT_BYTES]  # truncate a pathologically large root .tex
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _parse_tex(path: Path) -> dict:
    raw = _read_text_tolerant(path)
    # Comments are stripped inside _inline_inputs (before each \input scan), so a
    # commented-out \input is never followed.
    raw = _inline_inputs(raw, path.parent)

    title_raw, _ = _extract_command(raw, "title")
    title = _strip_tex(title_raw) if title_raw else ""

    abm = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", raw, re.S)
    abstract = _strip_tex(abm.group(1)) if abm else ""

    # Restrict to the document body when delimiters are present.
    dm = re.search(r"\\begin\{document\}", raw)
    body = raw[dm.end():] if dm else raw
    em = re.search(r"\\end\{document\}", body)
    if em:
        body = body[: em.start()]

    # Drop content already captured or irrelevant to topic extraction.
    body = re.sub(r"\\begin\{abstract\}.*?\\end\{abstract\}", " ", body, flags=re.S)
    body = re.sub(r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}", " ", body, flags=re.S)
    body = re.sub(r"\\bibliography\s*\{[^}]*\}", " ", body)
    body = re.sub(r"\\bibliographystyle\s*\{[^}]*\}", " ", body)
    body = re.sub(r"\\printbibliography\b", " ", body)

    # Footnotes carry substantive claims and references in many manuscripts.
    body, fn_raw = _extract_all_footnotes(body)
    footnotes = [t for t in (_strip_tex(f) for f in fn_raw) if t]

    raw_sections = _split_sections_tex(body)
    sections = []
    for s in raw_sections:
        heading = _strip_tex(s["heading"]) or s["heading"]
        text = _strip_tex(s["text"])
        if text:
            sections.append({"heading": heading, "text": text})

    return {
        "title": title,
        "abstract": abstract,
        "sections": sections,
        "footnotes": footnotes,
    }


def _inline_inputs(text: str, base: Path, depth: int = 0,
                   seen: set | None = None, root: Path | None = None,
                   budget: list | None = None) -> str:
    r"""Inline \input / \include targets, contained to the manuscript tree.

    Security: each target is resolved and must remain under ``root`` (the top
    manuscript directory) — absolute paths and ``..`` escapes are refused — and
    depth, file-count, per-file-size, and total-expanded-size caps bound it.
    Comments are stripped first so a commented-out \input is ignored.
    """
    if root is None:
        root = base.resolve()
    if seen is None:
        seen = set()
    if budget is None:
        budget = [_MAX_TEX_TOTAL_BYTES]
    if depth > _MAX_TEX_DEPTH:
        return text

    text = _strip_comments(text)

    def repl(m: re.Match) -> str:
        name = m.group(1).strip()
        if not name:
            return ""
        for cand in (name, name + ".tex"):
            try:
                p = (base / cand).resolve()
            except Exception:
                continue
            try:
                p.relative_to(root)            # must stay within the manuscript tree
            except ValueError:
                continue                        # absolute path or '..' escape -> refuse
            if not p.is_file() or str(p) in seen:
                continue
            if len(seen) >= _MAX_TEX_INCLUDE_FILES:
                return ""
            try:
                if p.stat().st_size > _MAX_TEX_INCLUDE_BYTES:
                    return ""
            except Exception:
                continue
            seen.add(str(p))
            try:
                sub = _read_text_tolerant(p)
            except Exception:
                return ""
            budget[0] -= len(sub)
            if budget[0] < 0:
                return ""
            return _inline_inputs(sub, p.parent, depth + 1, seen, root, budget)
        return ""  # missing/!contained target -> drop quietly

    return re.sub(r"\\(?:input|include)\s*\{([^}]*)\}", repl, text)


def _strip_comments(text: str) -> str:
    """Remove TeX line comments while preserving escaped percent signs."""
    out = []
    for line in text.split("\n"):
        res = []
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "%" and (i == 0 or line[i - 1] != "\\"):
                break
            res.append(ch)
            i += 1
        out.append("".join(res))
    return "\n".join(out)


def _braced_after(text: str, open_idx: int) -> tuple[str, int]:
    """Given the index of an opening '{', return (inner_content, index_after_close).

    Honours brace nesting and skips escaped braces (``\\{`` / ``\\}``).
    """
    depth = 0
    out: list[str] = []
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(text[i + 1])
            i += 2
            continue
        if ch == "{":
            depth += 1
            i += 1
            if depth == 1:
                continue           # don't include the outermost brace
            out.append(ch)
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return "".join(out), i + 1
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), n


# Optional bracket arg, e.g. \section[Short]{Long} or \footnote[1]{...}
_OPT = r"\s*(?:\[[^\]]*\])?\s*"


def _extract_command(text: str, cmd: str) -> tuple[str | None, tuple[int, int] | None]:
    """Return (content, span) for the first ``\\cmd[...]{...}`` (brace-matched)."""
    m = re.search(r"\\" + cmd + _OPT + r"\{", text)
    if not m:
        return None, None
    content, end = _braced_after(text, m.end() - 1)
    return content, (m.start(), end)


def _extract_all_footnotes(text: str) -> tuple[str, list[str]]:
    """Pull every ``\\footnote[...]{...}`` out of ``text`` (brace-matched)."""
    notes: list[str] = []
    out: list[str] = []
    i = 0
    pat = re.compile(r"\\footnote" + _OPT + r"\{")
    while True:
        m = pat.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i: m.start()])
        content, end = _braced_after(text, m.end() - 1)
        notes.append(content)
        i = end
    return "".join(out), notes


_HEADING_RE = re.compile(
    r"\\(section|subsection|subsubsection|chapter|paragraph)\*?" + _OPT + r"\{"
)


def _split_sections_tex(body: str) -> list[dict]:
    """Split a raw-TeX body into sections at sectioning commands."""
    matches = []
    for m in _HEADING_RE.finditer(body):
        title, end = _braced_after(body, m.end() - 1)
        matches.append((m.start(), end, m.group(1), title.strip()))

    if not matches:
        return [{"heading": "(body)", "text": body.strip()}]

    sections = []
    pre = body[: matches[0][0]].strip()
    if pre:
        sections.append({"heading": "(intro)", "text": pre})
    for idx, (_start, end, _lvl, title) in enumerate(matches):
        nxt = matches[idx + 1][0] if idx + 1 < len(matches) else len(body)
        sections.append({"heading": title, "text": body[end:nxt].strip()})
    return sections


def _strip_tex(text: str) -> str:
    """Pragmatic de-TeX: keep prose, drop markup, math, and citation keys."""
    if not text:
        return ""

    # Math
    text = re.sub(r"\$\$.*?\$\$", " ", text, flags=re.S)
    text = re.sub(r"\$.*?\$", " ", text, flags=re.S)
    text = re.sub(r"\\\[.*?\\\]", " ", text, flags=re.S)
    text = re.sub(r"\\\(.*?\\\)", " ", text, flags=re.S)
    for env in ("equation", "equation*", "align", "align*", "gather", "gather*",
                "multline", "multline*", "displaymath", "eqnarray", "eqnarray*"):
        text = re.sub(
            r"\\begin\{" + re.escape(env) + r"\}.*?\\end\{" + re.escape(env) + r"\}",
            " ", text, flags=re.S,
        )

    # Citation / cross-reference commands: drop entirely (keys are not prose)
    text = re.sub(
        r"\\(?:cite[a-zA-Z]*|[Cc]ite[tp]?|nocite|ref|eqref|autoref|cref|Cref|pageref|label)"
        r"\s*(?:\[[^\]]*\])?\{[^}]*\}",
        " ",
        text,
    )

    # Unwrap text-bearing commands: \textbf{x} -> x  (repeat for light nesting)
    text_cmds = (r"(?:textbf|textit|texttt|textsc|textrm|textsf|emph|underline|uline|"
                 r"mbox|text|enquote|so|highlight)")
    for _ in range(5):
        text = re.sub(r"\\" + text_cmds + r"\{([^{}]*)\}", r"\1", text)

    # Commands whose argument should be discarded
    text = re.sub(
        r"\\(?:includegraphics|input|include|usepackage|documentclass|setlength|"
        r"geometry|hypersetup|thanks|footnote|footnotetext|vspace|hspace|caption|"
        r"label|date)\s*(?:\[[^\]]*\])?\{[^}]*\}",
        " ",
        text,
    )

    # Environment delimiters (keep inner prose for itemize/enumerate/etc.)
    text = re.sub(r"\\(?:begin|end)\{[^}]*\}", " ", text)
    text = re.sub(r"\\item\b", " ", text)

    # Any remaining backslash command + optional bracket arg
    text = re.sub(r"\\[a-zA-Z@]+\*?\s*(?:\[[^\]]*\])?", " ", text)
    # Escaped symbols (\&, \%, \_, \$ ...)
    text = re.sub(r"\\([&%_$#{}])", r"\1", text)
    text = re.sub(r"\\[^a-zA-Z]", " ", text)

    text = text.replace("~", " ")
    text = re.sub(r"[{}]", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ===========================================================================
# Salient text for the extractor
# ===========================================================================

def build_salient_text(parsed: dict, max_chars: int = 30000) -> str:
    """Assemble the most search-relevant text, hard-capped for the LLM prompt.

    Title, abstract, and section headings come first; section bodies fill the
    remaining budget; a footnote excerpt is appended. The whole result is then
    truncated to ``max_chars`` so the cap always holds.
    """
    head_parts = []
    if parsed.get("title"):
        head_parts.append(f"TITLE: {parsed['title']}")
    if parsed.get("abstract"):
        head_parts.append(f"ABSTRACT:\n{parsed['abstract']}")

    headings = [
        s["heading"] for s in parsed.get("sections", [])
        if s.get("heading") and not s["heading"].startswith("(")
    ]
    if headings:
        head_parts.append("SECTION HEADINGS: " + " | ".join(headings))

    head = "\n\n".join(head_parts)

    fns = parsed.get("footnotes", [])
    fn_reserve = 1600 if fns else 0
    budget = max(0, max_chars - len(head) - fn_reserve)

    body_segs = []
    for s in parsed.get("sections", []):
        body_segs.append(f"\n\n## {s['heading']}\n{s['text']}")
    body = "".join(body_segs)
    if len(body) > budget:
        body = body[:budget]

    fn_text = ""
    if fns:
        fn_text = "\n\nFOOTNOTES (excerpt):\n" + (" • ".join(fns))[:1500]

    return (head + body + fn_text).strip()[:max_chars]


# ===========================================================================
# CLI (inspection / testing)
# ===========================================================================

def main() -> None:
    ap = argparse.ArgumentParser(description="Inspect a parsed manuscript.")
    ap.add_argument("manuscript", help="Path to .docx or .tex")
    ap.add_argument("--json", action="store_true", help="Print full parsed JSON")
    ap.add_argument("--salient", action="store_true", help="Print the salient text")
    ap.add_argument("--max-chars", type=int, default=30000)
    args = ap.parse_args()

    parsed = parse_manuscript(args.manuscript)

    if args.json:
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
        return
    if args.salient:
        print(build_salient_text(parsed, args.max_chars))
        return

    print(f"format     : {parsed['format']}")
    print(f"title      : {parsed['title'][:120]}")
    print(f"abstract   : {len(parsed['abstract'])} chars")
    print(f"sections   : {len(parsed['sections'])}")
    for s in parsed["sections"]:
        print(f"   - {s['heading'][:70]:<70} ({len(s['text'])} chars)")
    print(f"footnotes  : {len(parsed['footnotes'])}")
    print(f"salient    : {parsed['n_chars']} chars")


if __name__ == "__main__":
    main()
