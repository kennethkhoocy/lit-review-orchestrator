#!/usr/bin/env python3
"""Stage 1 — Undermind.ai deep search driver (Playwright, Classic mode).

Drives the Undermind "Classic" search end to end and returns the papers it finds,
enriched and saved as ``<stem>.json`` + ``<stem>.ris`` for dedup and screening.

Validated flow (June 2026 UI):
    app.undermind.ai (session assumed)  ->  sidebar **Classic**  ->  **Search**
    -> type the brief into "I want to find..."  ->  answer Undermind's clarifying
    questions (via Claude)  ->  click **Generate Research Report**  ->  wait for the
    report  ->  **Export -> RIS**  ->  parse + enrich (undermind_ingest).

Login model: a dedicated persistent browser profile (``--user-data-dir``,
default ``~/.undermind-profile``). Seed it once with::

    python undermind_search.py --login            # headed; sign in, then press Enter

Thereafter the session cookie persists and headless runs reuse it. If no valid
session is found and ``--login`` was not used, the driver prints the
``UNDERMIND_DEFERRED`` sentinel, writes empty results, and exits 0 so the rest of
the pipeline still completes (the orchestrator marks the stage "deferred").

CLI (matches the orchestrator's call; extra flags are for testing):
    python undermind_search.py --brief-file undermind_brief.txt \
        -o stage1_undermind.json --debug-dir debug_undermind
    python undermind_search.py --query "dual-class shares cost of equity" --headed
    python undermind_search.py --login --headed

Requires: ANTHROPIC_API_KEY (clarifying-question answers); playwright + a Chrome
channel (``playwright install chromium`` or system Chrome).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

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

# Bundled ingest module (same directory). The actual import is deferred into the
# guarded section of run() so that a missing ingest dependency defers gracefully
# (sentinel + empty output, exit 0) instead of crashing at module load.
sys.path.insert(0, str(Path(__file__).resolve().parent))

SENTINEL = "UNDERMIND_DEFERRED"
APP_URL = "https://app.undermind.ai"
DEFAULT_PROFILE = Path.home() / ".undermind-profile"
DEFAULT_MODEL = "claude-sonnet-4-6"

# Timeouts (seconds)
PAGE_LOAD_MS = 45_000
SEARCH_TIMEOUT_S = 1200          # deep search can take 10+ minutes
POLL_INTERVAL_S = 5
STREAM_POLL_S = 1.5
STREAM_STABLE_CHECKS = 4
MAX_CONVERSATION_TURNS = 6


# ── small utilities ───────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(msg, flush=True)


def _shot(page, debug_dir: Path | None, name: str) -> None:
    if not debug_dir:
        return
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(debug_dir / f"{name}.png"), full_page=True)
    except Exception:
        pass


def _dump_html(page, debug_dir: Path | None, name: str) -> None:
    if not debug_dir:
        return
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def _scrub_credentials(page) -> None:
    """Blank any email/password inputs so error screenshots/HTML never leak them."""
    try:
        page.evaluate(
            """() => {
                document.querySelectorAll('input#password, input[type=password]')
                    .forEach(e => { e.value = ''; });
                document.querySelectorAll('input#email, input[type=email]')
                    .forEach(e => { e.value = ''; });
            }"""
        )
    except Exception:
        pass


# ── login / session ───────────────────────────────────────────────────────────

def _logged_in(page) -> bool:
    """Heuristic: the Classic entry point is only present once authenticated."""
    try:
        if page.get_by_role("button", name="Classic").count() > 0:
            return True
    except Exception:
        pass
    try:
        # "Login" / "Sign in" affordances imply we are NOT authenticated
        for name in ("Log in", "Login", "Sign in", "Sign In"):
            if page.get_by_role("button", name=name).count() > 0:
                return False
            if page.get_by_role("link", name=name).count() > 0:
                return False
    except Exception:
        pass
    # Fall back to "looks logged in if the app shell rendered"
    try:
        return page.get_by_role("link", name="Projects").count() > 0
    except Exception:
        return False


def _env_quote(val: str) -> str:
    """Double-quote a dotenv value when it needs it (spaces, #, quotes, edges)."""
    needs = (val == "" or val != val.strip()
             or any(c in val for c in (" ", "#", '"', "'", "\\")))
    if not needs:
        return val
    return '"' + val.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _save_credentials_to_env(email: str, password: str) -> None:
    """Persist Undermind credentials to ~/.lit-review-pipeline.env (gitignored).

    Replaces the first occurrence of each key (matching plain or ``export KEY=``
    lines), drops later duplicates, preserves all other lines, and quotes values
    that would otherwise be misread by python-dotenv.
    """
    env = Path.home() / ".lit-review-pipeline.env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    kv = {"UNDERMIND_EMAIL": email, "UNDERMIND_PASSWORD": password}
    seen, out = set(), []
    for ln in lines:
        raw_key = ln.split("=", 1)[0].strip() if "=" in ln else ""
        key = raw_key[7:].strip() if raw_key.startswith("export ") else raw_key
        if key in kv:
            if key not in seen:
                out.append(f"{key}={_env_quote(kv[key])}")
                seen.add(key)
            # else: drop a duplicate key line
        else:
            out.append(ln)
    for key, val in kv.items():
        if key not in seen:
            out.append(f"{key}={_env_quote(val)}")
    env.write_text("\n".join(out) + "\n", encoding="utf-8")


def _get_credentials(prompt_if_missing: bool = False) -> tuple[str, str]:
    """Read UNDERMIND_EMAIL/PASSWORD from env; optionally prompt and persist."""
    import getpass
    email = os.environ.get("UNDERMIND_EMAIL", "").strip()
    password = os.environ.get("UNDERMIND_PASSWORD", "")
    if (not email or not password) and prompt_if_missing:
        _log("Undermind credentials are needed; they will be saved to "
             "~/.lit-review-pipeline.env for future runs.")
        if not email:
            try:
                email = input("  Undermind email: ").strip()
            except EOFError:
                email = ""
        if not password:
            try:
                password = getpass.getpass("  Undermind password: ")
            except Exception:
                password = ""
        if email and password:
            _save_credentials_to_env(email, password)
            _log("  Saved credentials to ~/.lit-review-pipeline.env")
    return email, password


def login_with_credentials(page, email: str, password: str, debug_dir: Path | None) -> bool:
    """Open the Login modal, enter email + password, submit, and verify."""
    try:
        page.get_by_role("button", name="Login").first.click(timeout=15_000)
    except Exception:
        pass
    page.wait_for_timeout(1200)
    _shot(page, debug_dir, "00a_login_modal")  # before filling (no secrets on screen)
    page.locator("input#email").first.fill(email)
    page.locator("input#password").first.fill(password)
    submitted = False
    for sel in ('div[role="dialog"] button[type="submit"]',
                'button[type="submit"]:has-text("Login")'):
        try:
            b = page.locator(sel)
            if b.count() > 0 and b.last.is_enabled():
                b.last.click()
                submitted = True
                break
        except Exception:
            continue
    if not submitted:
        try:
            page.locator("input#password").first.press("Enter")
        except Exception:
            pass
    try:
        page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_MS)
    except Exception:
        pass
    page.wait_for_timeout(3500)
    return _logged_in(page)


def do_login(page, debug_dir: Path | None) -> None:
    """First-run setup / verify: capture credentials (if needed) and sign in (headed)."""
    email, password = _get_credentials(prompt_if_missing=True)
    _log("Navigating to Undermind...")
    page.goto(APP_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_MS)
    page.wait_for_timeout(2500)
    _shot(page, debug_dir, "login_landing")
    if _logged_in(page):
        _log("Already logged in — the profile has a valid session.")
        return
    if not email or not password:
        _log("No credentials available; cannot log in.")
        return
    ok = login_with_credentials(page, email, password, debug_dir)
    _log("Login confirmed; session saved to the profile." if ok
         else "WARNING: could not confirm login — check the credentials.")
    _scrub_credentials(page)  # blank any still-visible login fields before the shot
    _shot(page, debug_dir, "login_done")


# ── navigation ────────────────────────────────────────────────────────────────

def navigate_to_classic_search(page, debug_dir: Path | None) -> None:
    """Sidebar: click 'Classic', then 'Search'; wait for the composer."""
    # Click "Classic" to reveal the Search / History / Alerts submenu.
    classic = page.get_by_role("button", name="Classic")
    classic.first.wait_for(state="visible", timeout=20_000)
    classic.first.click()
    page.wait_for_timeout(800)
    _shot(page, debug_dir, "01_classic_open")

    # Click the "Search" submenu link (exact, to avoid "Search references").
    search = page.get_by_role("link", name="Search", exact=True)
    if search.count() == 0:
        search = page.get_by_role("link", name=re.compile(r"^Search$", re.I))
    search.first.click()
    page.wait_for_timeout(1200)

    # Composer should be present.
    composer = _composer(page)
    composer.wait_for(state="visible", timeout=20_000)
    _shot(page, debug_dir, "02_search_ready")
    _log("  Classic search page ready.")


def _composer(page):
    """Locate the visible 'I want to find...' research-goal textarea."""
    box = page.get_by_placeholder(re.compile(r"I want to find", re.I))
    if box.count() == 0:
        box = page.locator("textarea:visible")
    return box.first


def _submit_composer(page) -> None:
    """Submit the composer: prefer the send button, fall back to Enter."""
    # 1) An explicit send/submit button next to the textarea.
    for getter in (
        lambda: page.get_by_role("button", name=re.compile(r"send", re.I)),
        lambda: page.locator('form button[type="submit"]'),
        lambda: page.locator('button[aria-label*="end" i]'),  # "Send"
    ):
        try:
            btn = getter()
            if btn.count() > 0 and btn.first.is_enabled():
                btn.first.click()
                return
        except Exception:
            continue
    # 2) The icon button immediately after the mic.
    try:
        mic = page.get_by_role("button", name=re.compile(r"voice recording", re.I))
        if mic.count() > 0:
            sib = mic.first.locator("xpath=following::button[1]")
            if sib.count() > 0:
                sib.first.click()
                return
    except Exception:
        pass
    # 3) Keyboard fallback.
    try:
        _composer(page).press("Enter")
    except Exception:
        page.keyboard.press("Enter")


def submit_brief(page, brief: str, debug_dir: Path | None) -> None:
    box = _composer(page)
    box.click()
    box.fill(brief)
    page.wait_for_timeout(400)
    _submit_composer(page)
    _log(f"  Submitted brief ({len(brief)} chars).")
    page.wait_for_timeout(2500)
    _shot(page, debug_dir, "03_brief_submitted")


# ── clarifying-question conversation ──────────────────────────────────────────

def _generate_report_button(page):
    return page.get_by_role("button", name=re.compile(r"Generate Research Report", re.I))


def _latest_assistant_text(page) -> str:
    """Read the latest assistant (left-aligned) message / clarifying prompt.

    Prefers the last substantial ``.prose`` that is NOT inside a right-aligned
    (user) bubble, so we never feed the user's own brief/answers back to Claude.
    """
    try:
        text = page.evaluate(
            """() => {
                const isUser = (el) => !!el.closest('[class*="justify-end"], [class*="items-end"]');
                const proses = Array.from(document.querySelectorAll('.prose'));
                for (let i = proses.length - 1; i >= 0; i--) {
                    const el = proses[i];
                    const t = (el.innerText || '').trim();
                    if (t.length > 40 && !isUser(el)) return t;
                }
                for (let i = proses.length - 1; i >= 0; i--) {
                    const t = (proses[i].innerText || '').trim();
                    if (t.length > 40) return t;
                }
                return '';
            }"""
        )
        return (text or "").strip()
    except Exception:
        return ""


def _wait_stable_assistant(page, timeout_s: int = 90) -> str:
    prev, stable, start = "", 0, time.time()
    while time.time() - start < timeout_s:
        page.wait_for_timeout(int(STREAM_POLL_S * 1000))
        if _generate_report_button(page).count() > 0:
            return _latest_assistant_text(page)
        cur = _latest_assistant_text(page)
        if cur and cur == prev:
            stable += 1
            if stable >= STREAM_STABLE_CHECKS:
                return cur
        else:
            stable, prev = 0, cur
    return prev


def _ask_claude(client, brief: str, history: list[dict], question: str, model: str) -> str:
    system = (
        "You are answering Undermind.ai's clarifying questions on behalf of a "
        "researcher, so that Undermind can run the best possible literature search. "
        f'The researcher\'s brief is:\n"{brief}"\n'
        "Answer each clarifying question concisely and decisively in 1-3 sentences, "
        "grounded in the brief. If asked to choose among options, pick the option(s) "
        "that best match the brief and say so plainly. Do not ask questions back."
    )
    messages = list(history)
    messages.append({"role": "user", "content": question})
    resp = client.messages.create(model=model, max_tokens=350, system=system,
                                  messages=messages)
    return resp.content[0].text.strip()


def _get_answer_via_handoff(answers_dir: Path, turn: int, question: str, brief: str,
                            timeout_s: int = 900) -> str:
    """Agent-in-the-loop: write the clarifying question to a file and wait for the
    orchestrator to drop an answer file. Returns the answer, or "" on timeout.

    No Anthropic API is used — the reasoning runs in the orchestrator's subagent
    (Sonnet by default, Opus when all_opus is set), per the model-routing policy."""
    answers_dir.mkdir(parents=True, exist_ok=True)
    req = answers_dir / f"clarify_request_{turn}.json"
    ans = answers_dir / f"clarify_answer_{turn}.json"
    try:
        if ans.exists():
            ans.unlink()
    except Exception:
        pass
    req.write_text(json.dumps({"turn": turn, "question": question, "brief": brief},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"  [handoff] Wrote {req.name}; waiting up to {timeout_s}s for {ans.name} (subagent answer)...")
    start = time.time()
    while time.time() - start < timeout_s:
        if ans.exists():
            try:
                answer = (json.loads(ans.read_text(encoding="utf-8")).get("answer") or "").strip()
            except Exception:
                answer = ""
            if answer:
                _log(f"  [handoff] Received answer for turn {turn}.")
                return answer
        time.sleep(5)
        el = int(time.time() - start)
        if el and el % 30 == 0:
            _log(f"    [handoff] still waiting for {ans.name} ({el}s)...")
    _log(f"  [handoff] No answer within {timeout_s}s.")
    return ""


def answer_clarifications(page, brief: str, model: str, debug_dir: Path | None,
                          answers_dir: Path | None = None) -> None:
    """Loop: answer Undermind's clarifying questions until the topic is proposed.

    Two modes. When ``answers_dir`` is given, each question is handed to the
    orchestrator's subagent through files (no API) — the agent-in-the-loop path
    (Sonnet by default, Opus when all_opus). Otherwise it is answered by the
    Anthropic API (Sonnet), the autonomous fallback."""
    client = None
    if answers_dir is None:
        import anthropic
        client = anthropic.Anthropic()
    history: list[dict] = []

    def _norm(s: str) -> str:
        return " ".join((s or "").split())[:300].lower()

    seen = {_norm(brief)}  # never treat our own brief/answers as a question

    for turn in range(MAX_CONVERSATION_TURNS):
        if _generate_report_button(page).count() > 0:
            _log("  Proposed search topic ready (Generate Research Report present).")
            return
        question = _wait_stable_assistant(page, timeout_s=120)
        if _generate_report_button(page).count() > 0:
            _log("  Proposed search topic ready.")
            return
        if not question:
            _log("  No clarifying text detected; proceeding.")
            return
        if _norm(question) in seen:
            # The latest text is our own brief/answer echoed back — Undermind has
            # not responded yet; wait another cycle rather than answering ourselves.
            page.wait_for_timeout(2500)
            continue
        _log(f"  [Q{turn + 1}] {question[:160].replace(chr(10), ' ')}...")
        if answers_dir is not None:
            answer = _get_answer_via_handoff(answers_dir, turn + 1, question, brief)
            if not answer:
                answer = ("The brief is complete and correct; please proceed with the "
                          "search exactly as described in it.")
                _log("  [handoff] Using safe fallback answer (no agent response in time).")
        else:
            answer = _ask_claude(client, brief, history, question, model)
        _log(f"  [A{turn + 1}] {answer[:160].replace(chr(10), ' ')}...")
        history += [{"role": "user", "content": question},
                    {"role": "assistant", "content": answer}]
        seen.add(_norm(question))
        seen.add(_norm(answer))
        box = _composer(page)
        box.click()
        box.fill(answer)
        page.wait_for_timeout(300)
        _submit_composer(page)
        _shot(page, debug_dir, f"04_answer_turn_{turn + 1}")
        page.wait_for_timeout(2500)
    _log("  Reached max conversation turns; proceeding to launch.")


def launch_search(page, debug_dir: Path | None) -> None:
    """Click 'Generate Research Report' (and confirm a dialog if one appears)."""
    btn = _generate_report_button(page)
    btn.first.wait_for(state="visible", timeout=60_000)
    btn.first.scroll_into_view_if_needed()
    btn.first.click()
    page.wait_for_timeout(1500)
    # Optional confirmation dialog.
    for name in ("Launch", "Generate", "Confirm", "Start"):
        try:
            dlg = page.locator(f'[role="dialog"] button:has-text("{name}"), '
                               f'[role="alertdialog"] button:has-text("{name}")')
            if dlg.count() > 0 and dlg.first.is_visible():
                dlg.first.click()
                page.wait_for_timeout(1000)
                break
        except Exception:
            continue
    _log("  Search launched.")
    _shot(page, debug_dir, "05_search_launched")


def _export_locator(page):
    """Return the first visible references 'Export' control, or None.

    Tries the accessible-name button, an aria-label fallback, and a text match —
    so the report-ready signal survives minor relabelling of the Export control.
    """
    candidates = (
        page.get_by_role("button", name=re.compile(r"export", re.I)),
        page.locator('button[aria-label*="Export" i]'),
        page.locator('button:has-text("Export")'),
    )
    for c in candidates:
        try:
            n = c.count()
        except Exception:
            continue
        for i in range(n):
            try:
                if c.nth(i).is_visible():
                    return c.nth(i)
            except Exception:
                continue
    return None


def wait_for_report(page, debug_dir: Path | None) -> None:
    """Poll until the report is ready (a visible Export control appears)."""
    _log("  Waiting for the deep search to complete (this can take minutes)...")
    start = time.time()
    while time.time() - start < SEARCH_TIMEOUT_S:
        page.wait_for_timeout(int(POLL_INTERVAL_S * 1000))
        elapsed = int(time.time() - start)
        try:
            if _export_locator(page) is not None:
                _log(f"  Report ready after {elapsed}s.")
                _shot(page, debug_dir, "06_report_ready")
                return
        except Exception:
            pass
        if elapsed and elapsed % 30 == 0:
            _log(f"    still searching... ({elapsed}s)")
    _shot(page, debug_dir, "06_report_timeout")
    raise TimeoutError(f"Report not ready within {SEARCH_TIMEOUT_S}s")


def export_references(page, dest: Path, fmt: str, debug_dir: Path | None) -> Path:
    """Open the Export menu, choose the format, save the download to ``dest``.

    ``fmt`` is "bibtex" or "ris"; the menu exposes CSV / BibTeX / RIS / Chicago.
    Playwright captures the download directly, so no native Save-As dialog appears.
    """
    label = "BibTeX" if fmt == "bibtex" else "RIS"
    export_btn = _export_locator(page) or page.get_by_role(
        "button", name=re.compile(r"export", re.I)).first
    export_btn.click()
    page.wait_for_timeout(800)
    _shot(page, debug_dir, "07_export_menu")
    option = page.get_by_role("menuitem", name=re.compile(rf"^{label}$", re.I))
    if option.count() == 0:
        option = page.get_by_text(re.compile(rf"^{label}$", re.I))
    with page.expect_download(timeout=60_000) as dl:
        option.first.click()
    download = dl.value
    dest.parent.mkdir(parents=True, exist_ok=True)
    download.save_as(str(dest))
    _log(f"  Exported {label} -> {dest}")
    return dest


# ── orchestration ─────────────────────────────────────────────────────────────

def _load_brief(args) -> str:
    if args.brief_file:
        p = Path(args.brief_file)
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace").strip()
    if args.query:
        return args.query.strip()
    if args.brief:
        return args.brief.strip()
    return ""


def _defer(output: Path, message: str) -> None:
    """Graceful no-op: emit sentinel, write empty outputs, exit 0."""
    _log(f"{SENTINEL}: {message}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("[]", encoding="utf-8")
    # Match the success path's BibTeX sibling so a deferred run clears a stale .bib.
    output.with_suffix(".bib").write_text("", encoding="utf-8")
    sys.exit(0)


def run(args) -> None:
    output = Path(args.output)
    debug_dir = Path(args.debug_dir) if args.debug_dir else None
    user_data_dir = Path(args.user_data_dir).expanduser()
    headed = bool(args.headed or args.login)

    brief = "" if args.login else _load_brief(args)
    if not args.login and not brief:
        _defer(output, "No brief supplied (need --brief-file/--query).")
    answers_dir = Path(args.answers_dir).expanduser() if args.answers_dir else None
    if (not args.login and answers_dir is None
            and not os.environ.get("ANTHROPIC_API_KEY")):
        _defer(output, "ANTHROPIC_API_KEY not set (needed for API-mode clarifying answers; "
                       "or pass --answers-dir for agent-in-the-loop mode).")

    fmt = getattr(args, "format", "bibtex")
    ext = ".bib" if fmt == "bibtex" else ".ris"
    raw_export = (debug_dir or output.parent) / f"undermind_export{ext}"

    # From here, any failure (Playwright import, browser launch, the flow itself)
    # degrades to a graceful defer (exit 0) so the orchestrator marks the stage
    # "deferred" and the rest of the pipeline still runs.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            launch_kwargs = dict(user_data_dir=str(user_data_dir), headless=not headed,
                                 accept_downloads=True, viewport={"width": 1480, "height": 1000})
            try:
                context = p.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
            except Exception:
                context = p.chromium.launch_persistent_context(**launch_kwargs)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                if args.login:
                    do_login(page, debug_dir)
                    return

                _log("Opening Undermind...")
                page.goto(APP_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_MS)
                page.wait_for_timeout(3000)
                _shot(page, debug_dir, "00_landing")
                if not _logged_in(page):
                    email, password = _get_credentials(prompt_if_missing=False)
                    if not email or not password:
                        _defer(output, "Not logged in and no UNDERMIND_EMAIL/PASSWORD in "
                                       "~/.lit-review-pipeline.env. Run:  undermind_search.py --login")
                    _log("Logging in with stored credentials...")
                    if not login_with_credentials(page, email, password, debug_dir):
                        _defer(output, "Automated login failed (check credentials / debug-dir).")
                    _log("  Logged in.")

                navigate_to_classic_search(page, debug_dir)
                submit_brief(page, brief, debug_dir)
                answer_clarifications(page, brief, args.model, debug_dir, answers_dir=answers_dir)
                launch_search(page, debug_dir)
                wait_for_report(page, debug_dir)
                export_references(page, raw_export, fmt, debug_dir)
            except SystemExit:
                raise  # a deliberate _defer() — let it exit 0 cleanly
            except Exception as e:
                _scrub_credentials(page)  # never leak creds into the dumps below
                _shot(page, debug_dir, "99_error")
                _dump_html(page, debug_dir, "99_error")
                _log(f"ERROR: {e}")
                _defer(output, f"Driver failed before export ({type(e).__name__}). "
                               "See debug-dir screenshots.")
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except SystemExit:
        raise
    except Exception as e:
        _defer(output, f"Undermind driver could not start ({type(e).__name__}: {e}).")

    if args.login:
        return

    # Parse + enrich the exported references; an ingest failure (including a
    # missing ingest dependency at import time) also defers.
    try:
        from undermind_ingest import ingest  # noqa: E402
        if not raw_export.is_file() or raw_export.stat().st_size < 30:
            _defer(output, "Export produced no reference file.")
        _log("Parsing + enriching the exported references...")
        results = ingest(raw_export, output, enrich=not args.no_enrich,
                         source="undermind", sibling="bibtex")
        if not results:
            _defer(output, "Export ingested to zero papers (empty/unparsable response).")
        _log(f"Saved {len(results)} papers to {output}")
    except SystemExit:
        raise
    except Exception as e:
        _defer(output, f"Ingest of the exported references failed ({type(e).__name__}: {e}).")


def main() -> None:
    ap = argparse.ArgumentParser(description="Undermind.ai deep-search driver (Classic mode).")
    ap.add_argument("brief", nargs="?", default="", help="Brief text (or use --brief-file/--query)")
    ap.add_argument("--brief-file", default="", help="Path to a file containing the brief")
    ap.add_argument("--query", default="", help="Short query/brief (standalone use)")
    ap.add_argument("-o", "--output", default="stage1_undermind.json", help="Output JSON path")
    ap.add_argument("--debug-dir", default="", help="Directory for screenshots / HTML dumps")
    ap.add_argument("--user-data-dir", default=str(DEFAULT_PROFILE),
                    help=f"Persistent browser profile (default: {DEFAULT_PROFILE})")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="Claude model for clarifying answers in API/autonomous mode")
    ap.add_argument("--answers-dir", default="",
                    help="Agent-in-the-loop: write each clarifying question to this dir "
                         "(clarify_request_<n>.json) and wait for the orchestrator's subagent to "
                         "drop clarify_answer_<n>.json. No Anthropic API key needed in this mode.")
    ap.add_argument("--headed", action="store_true", help="Show the browser (debugging)")
    ap.add_argument("--login", action="store_true",
                    help="Headed first-run setup: capture credentials to env (if missing) and verify sign-in")
    ap.add_argument("--format", choices=("bibtex", "ris"), default="bibtex",
                    help="Format to export from Undermind (default: bibtex)")
    ap.add_argument("--no-enrich", action="store_true", help="Skip Crossref/OpenAlex enrichment")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
