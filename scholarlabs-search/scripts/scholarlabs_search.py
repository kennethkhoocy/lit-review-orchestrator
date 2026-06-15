#!/usr/bin/env python3
"""Stage 2 — Google Scholar Labs deep search driver (Playwright).

Drives Google Scholar Labs (``scholar.google.com/scholar_labs/search``) end to
end and returns the papers it surfaces, enriched and saved as ``<stem>.json`` +
``<stem>.bib`` for dedup and screening.

Validated flow (June 2026 UI):
    scholar_labs/search (signed in)  ->  type a detailed research QUESTION into
    "Ask Scholar"  ->  submit  ->  a /session/<id> opens and the model evaluates
    results ("Found N relevant results")  ->  for each result click **Cite** ->
    read the **BibTeX** export link  ->  fetch every link with the logged-in
    session  ->  parse + enrich (scholarlabs_ingest).

Scholar Labs takes a *detailed natural-language research question*, which is a
different input from Undermind's descriptive brief — Stage 0 writes it to
``scholarlabs_query.txt`` and the orchestrator passes it via ``--query-file``.

Login model: a dedicated persistent browser profile (``--user-data-dir``, default
``~/.scholar-profile``). Because Google blocks scripted password entry, seed the
session once, by hand, in a visible window::

    python scholarlabs_search.py --login        # headed; sign in (handle 2FA), done

Thereafter the session cookie persists and headless runs reuse it. If no valid
session is found, the driver prints the ``SCHOLARLABS_DEFERRED`` sentinel, writes
empty results, and exits 0 so the rest of the pipeline still completes (the
orchestrator marks the stage "deferred"). If a headless run hits a login/2FA
wall it also raises a desktop alert telling you to re-run ``--login``.

CLI (matches the orchestrator's call; extra flags are for testing):
    python scholarlabs_search.py --query-file scholarlabs_query.txt \
        -o stage2_scholarlabs.json --debug-dir debug_scholarlabs
    python scholarlabs_search.py --query "How do dual-class shares affect cost of equity?" --headed
    python scholarlabs_search.py --login --headed

Requires: playwright + a Chrome channel (``playwright install chromium`` or system
Chrome). ANTHROPIC_API_KEY is NOT needed (Scholar Labs asks no clarifying
questions, unlike Undermind).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
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

# Bundled ingest module (same directory). The import itself is deferred to the
# guarded ingest block in run() so an import-time dependency failure (e.g. the
# requests import inside scholarlabs_ingest) degrades via _defer() rather than
# crashing before any sentinel/empty output is written.
sys.path.insert(0, str(Path(__file__).resolve().parent))

SENTINEL = "SCHOLARLABS_DEFERRED"
TWOFA_NOTICE = "SCHOLARLABS_2FA"
APP_URL = "https://scholar.google.com/scholar_labs/search"
DEFAULT_PROFILE = Path.home() / ".scholar-profile"

# Timeouts
PAGE_LOAD_MS = 45_000
SEARCH_TIMEOUT_S = 300           # Scholar Labs usually answers in 30-90s
POLL_INTERVAL_S = 3
LOGIN_WAIT_S = 300               # manual sign-in / 2FA window
CITE_MODAL_TIMEOUT_MS = 20_000


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
                document.querySelectorAll('input[type=password], input[name=Passwd]')
                    .forEach(e => { e.value = ''; });
                document.querySelectorAll('input[type=email], #identifierId')
                    .forEach(e => { e.value = ''; });
            }"""
        )
    except Exception:
        pass


def _desktop_alert(message: str) -> None:
    """Best-effort, non-blocking Windows notification + a loud console line."""
    _log(f"{TWOFA_NOTICE}: {message}")
    if sys.platform != "win32":
        return
    safe = message.replace("'", "`'").replace('"', '`"')
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Warning;$n.Visible=$true;"
        f"$n.ShowBalloonTip(30000,'Scholar Labs sign-in','{safe}',"
        "[System.Windows.Forms.ToolTipIcon]::Warning);Start-Sleep -Seconds 12;$n.Dispose()"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


# ── credentials ────────────────────────────────────────────────────────────────

def _env_quote(val: str) -> str:
    needs = (val == "" or val != val.strip()
             or any(c in val for c in (" ", "#", '"', "'", "\\")))
    if not needs:
        return val
    return '"' + val.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _save_credentials_to_env(email: str, password: str) -> None:
    """Persist Scholar credentials to ~/.lit-review-pipeline.env (gitignored)."""
    env = Path.home() / ".lit-review-pipeline.env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    kv = {"SCHOLAR_EMAIL": email, "SCHOLAR_PASSWORD": password}
    seen, out = set(), []
    for ln in lines:
        raw_key = ln.split("=", 1)[0].strip() if "=" in ln else ""
        key = raw_key[7:].strip() if raw_key.startswith("export ") else raw_key
        if key in kv:
            if key not in seen:
                out.append(f"{key}={_env_quote(kv[key])}")
                seen.add(key)
        else:
            out.append(ln)
    for key, val in kv.items():
        if key not in seen:
            out.append(f"{key}={_env_quote(val)}")
    env.write_text("\n".join(out) + "\n", encoding="utf-8")


def _get_credentials(prompt_if_missing: bool = False) -> tuple[str, str]:
    """Read SCHOLAR_EMAIL/PASSWORD from env; optionally prompt and persist."""
    import getpass
    email = os.environ.get("SCHOLAR_EMAIL", "").strip()
    password = os.environ.get("SCHOLAR_PASSWORD", "")
    if (not email or not password) and prompt_if_missing:
        _log("Google credentials (for Scholar Labs) will be saved to "
             "~/.lit-review-pipeline.env for future runs.")
        if not email:
            try:
                email = input("  Google email: ").strip()
            except EOFError:
                email = ""
        if not password:
            try:
                password = getpass.getpass("  Google password: ")
            except Exception:
                password = ""
        if email and password:
            _save_credentials_to_env(email, password)
            _log("  Saved credentials to ~/.lit-review-pipeline.env")
    return email, password


# ── login / session ───────────────────────────────────────────────────────────

def _composer(page):
    """Locate the 'Ask Scholar' / 'Ask a follow-up' research-question box."""
    for loc in (
        page.get_by_role("textbox", name=re.compile(r"Ask Scholar|Ask a follow", re.I)),
        page.get_by_placeholder(re.compile(r"Ask Scholar|Ask a follow", re.I)),
        page.get_by_role("textbox"),
        page.locator('textarea, input[type="text"], div[contenteditable="true"]'),
    ):
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return page.locator("textarea")


def _logged_in(page) -> bool:
    """Heuristic: signed in if the search composer is present and no sign-in wall."""
    try:
        if "accounts.google.com" in (page.url or ""):
            return False
    except Exception:
        pass
    try:
        for name in ("Sign in", "Sign In", "Sign in to Scholar"):
            if page.get_by_role("link", name=name).count() > 0:
                return False
            if page.get_by_role("button", name=name).count() > 0:
                return False
    except Exception:
        pass
    try:
        return _composer(page).count() > 0
    except Exception:
        return False


def _needs_2fa(page) -> bool:
    try:
        url = page.url or ""
        # The password / identifier pages also live under /challenge/ but are
        # handled by filling their fields, so they are not 2FA.
        if "challenge/pwd" in url or "challenge/identifier" in url:
            return False
        if "/challenge/" in url or "/signin/v2/challenge" in url:
            return True
        body = page.locator("body").inner_text(timeout=3000)
        low = body.lower()
        for pat in ("2-step verification", "verify it's you", "verify it’s you",
                    "enter the code", "2-factor", "get a verification code",
                    "check your phone", "check your device", "tap yes on",
                    "couldn't sign you in", "this browser or app may not be secure"):
            if pat in low:
                return True
    except Exception:
        pass
    return False


def _wait_until_logged_in(page, timeout_s: int) -> bool:
    start = time.time()
    while time.time() - start < timeout_s:
        if _logged_in(page):
            return True
        page.wait_for_timeout(2500)
    return _logged_in(page)


def _click_google_next(page, btn_id: str) -> None:
    for sel in (f"#{btn_id} button", f"#{btn_id}",
                'button:has-text("Next")', '#identifierNext', '#passwordNext'):
        try:
            b = page.locator(sel)
            if b.count() > 0 and b.first.is_visible():
                b.first.click()
                return
        except Exception:
            continue
    try:
        page.keyboard.press("Enter")
    except Exception:
        pass


def _is_google_auth(page) -> bool:
    try:
        url = page.url or ""
    except Exception:
        url = ""
    return any(s in url for s in ("accounts.google.com", "/signin",
                                  "ServiceLogin", "accountchooser"))


def _on_labs_surface(page) -> bool:
    """True only if the page URL is actually the Scholar Labs surface.

    ``_logged_in`` accepts a generic composer/editor element, which can be true
    on a non-Labs page; this guards the ``reach_app`` 'ok' decision by also
    requiring the URL host to match APP_URL's host or the path to carry the
    known Labs path, so a stray signed-in page is not mistaken for results.
    """
    try:
        from urllib.parse import urlparse
        url = page.url or ""
        parsed = urlparse(url)
        app_host = urlparse(APP_URL).netloc
        return parsed.netloc == app_host or "/scholar_labs" in parsed.path
    except Exception:
        return False


def _is_captcha(page) -> bool:
    """True if Google served its 'unusual traffic' bot-block / CAPTCHA page."""
    try:
        if page.locator("#captcha, form#captcha-form, #recaptcha").count() > 0:
            return True
        body = page.locator("body").inner_text(timeout=2000).lower()
        return ("detected unusual traffic" in body
                or "our systems have detected" in body
                or "unusual traffic from your computer" in body)
    except Exception:
        return False


def _click_account_tile(page, email: str) -> bool:
    """On the Google account chooser, click the saved account to continue.

    The tile is ``div[role="link"][data-identifier="<email>"]``; clicks can be
    intercepted by inner nodes, so fall back to a scroll, then a JS-level click,
    then the first tile, then the email text.
    """
    sels = []
    if email:
        sels += [f'div[role="link"][data-identifier="{email}"]',
                 f'[data-identifier="{email}"]', f'[data-email="{email}"]']
    sels += ['div[role="link"][data-identifier]', 'li[data-identifier]',
             '[data-button-type="multipleChoiceIdentifier"]']
    for sel in sels:
        try:
            el = page.locator(sel)
            if el.count() == 0:
                continue
            try:
                el.first.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            el.first.click(timeout=6000)
            return True
        except Exception:
            try:
                if page.eval_on_selector(sel, "e => { e.click(); return true; }"):
                    return True
            except Exception:
                continue
    if email:
        try:
            el = page.get_by_text(email, exact=False)
            if el.count() > 0 and el.first.is_visible():
                el.first.click(timeout=6000)
                return True
        except Exception:
            pass
    return False


def _advance_google_signin(page, email: str, password: str,
                           debug_dir: Path | None, headless: bool) -> str:
    """Take one step through Google's sign-in. Returns a status token:
    'app' | 'captcha' | 'progressed' | '2fa' | 'stuck'.

    Input fields take priority over the account-chooser tile: the password page
    also renders an account chip carrying ``data-identifier``, and clicking that
    chip bounces back to the chooser (an infinite loop), so fill the visible
    field first and only fall back to a tile click on the chooser itself.
    """
    if _is_captcha(page):
        return "captcha"
    if _logged_in(page):
        return "app"
    if not _is_google_auth(page):
        return "stuck"
    _shot(page, debug_dir, "00a_signin")
    # Password step (highest priority — see docstring).
    try:
        pw = page.locator('input[type="password"], input[name="Passwd"]')
        if password and pw.count() > 0 and pw.first.is_visible():
            pw.first.fill(password)
            _click_google_next(page, "passwordNext")
            page.wait_for_timeout(3500)
            return "progressed"
    except Exception:
        pass
    # Identifier (email) step.
    try:
        em = page.locator('input[type="email"], #identifierId')
        if email and em.count() > 0 and em.first.is_visible():
            em.first.fill(email)
            _click_google_next(page, "identifierNext")
            page.wait_for_timeout(2500)
            return "progressed"
    except Exception:
        pass
    # Real 2FA / verification challenge (not the password or identifier page).
    if _needs_2fa(page):
        return "2fa"
    # Account chooser (no input fields present): pick the saved account.
    if _click_account_tile(page, email):
        page.wait_for_timeout(2500)
        return "progressed"
    return "stuck"


def reach_app(page, email: str, password: str,
              debug_dir: Path | None, headless: bool) -> str:
    """Drive from APP_URL to a usable Scholar Labs page.

    Handles the account chooser, identifier/password steps, and 2FA. Returns
    'ok' | 'captcha' | '2fa' | 'login_needed'. The reliable seeding path is a
    one-time headed ``--login``; Google blocks scripted password entry, so the
    scripted steps here are opportunistic.
    """
    last = "login_needed"
    for i in range(8):
        st = _advance_google_signin(page, email, password, debug_dir, headless)
        try:
            _log(f"  [auth {i + 1}] {st} :: {(page.url or '')[:90]}")
        except Exception:
            pass
        if st == "app" and _on_labs_surface(page):
            return "ok"
        if st == "captcha":
            _shot(page, debug_dir, "98_captcha")
            return "captcha"
        if st == "2fa":
            if headless:
                return "2fa"
            _log(f"  2FA / verification required — complete it in the browser; "
                 f"waiting up to {LOGIN_WAIT_S}s...")
            if _wait_until_logged_in(page, LOGIN_WAIT_S) and _on_labs_surface(page):
                return "ok"
            last = "2fa"
        elif st == "stuck":
            # The SPA may still be rendering, or there is nothing to act on yet.
            try:
                _composer(page).first.wait_for(state="visible", timeout=8000)
                if _logged_in(page) and _on_labs_surface(page):
                    return "ok"
            except Exception:
                pass
            if not _is_google_auth(page) and not _is_captcha(page):
                page.wait_for_timeout(1500)
                if _logged_in(page) and _on_labs_surface(page):
                    return "ok"
        page.wait_for_timeout(1000)
    if _is_captcha(page):
        return "captcha"
    return "ok" if (_logged_in(page) and _on_labs_surface(page)) else last


def do_login(page, debug_dir: Path | None) -> bool:
    """Headed first-run setup: capture credentials, sign in (manual 2FA), verify.

    On first run, missing SCHOLAR_EMAIL/SCHOLAR_PASSWORD are prompted for and
    written to ~/.lit-review-pipeline.env; if they are already set they pre-fill
    the form so you never re-enter them. Either way the persistent profile carries
    the session (complete any 2FA live in the window).
    """
    email, password = _get_credentials(prompt_if_missing=True)
    _log("Opening Google Scholar Labs...")
    page.goto(APP_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_MS)
    page.wait_for_timeout(2500)
    _shot(page, debug_dir, "login_landing")
    status = reach_app(page, email, password, debug_dir, headless=False)
    if status == "ok":
        _log("Sign-in confirmed; session saved to the profile.")
        _shot(page, debug_dir, "login_done")
        return True
    if status == "captcha":
        _log("Google served an 'unusual traffic' CAPTCHA. Wait a few minutes "
             "(let automated requests stop) and re-run --login.")
    _log("Finish signing in to Google in the browser window (handle any 2FA). "
         f"Waiting up to {LOGIN_WAIT_S}s...")
    ok = _wait_until_logged_in(page, LOGIN_WAIT_S)
    _log("Sign-in confirmed; session saved." if ok
         else "WARNING: could not confirm sign-in.")
    _shot(page, debug_dir, "login_done")
    return ok


# ── search ─────────────────────────────────────────────────────────────────────

def _fit_query(query: str, limit: int = 280) -> str:
    """Scholar Labs answers a single focused question and has a limited input; an
    over-long, multi-part query returns nothing. Collapse whitespace and, if the
    query is too long, reduce it to its first sentence (then a word-boundary cut)
    so a stray paragraph still searches as a question. The real fix is upstream —
    Stage 0 should emit a short question — this is only a safety net."""
    q = " ".join((query or "").split())
    if len(q) <= limit:
        return q
    first = re.split(r"(?<=[.?!])\s+", q)[0]
    if 0 < len(first) <= limit:
        return first
    return q[:limit].rsplit(" ", 1)[0]


def submit_query(page, query: str, debug_dir: Path | None) -> None:
    box = _composer(page).first
    box.wait_for(state="visible", timeout=20_000)
    box.click()
    fitted = _fit_query(query)
    if fitted != " ".join((query or "").split()):
        _log(f"  Query too long for Scholar Labs; trimmed {len(query)}->{len(fitted)} chars.")
    box.fill(fitted)
    page.wait_for_timeout(400)
    _shot(page, debug_dir, "02_query_typed")
    # Prefer an explicit send button; fall back to Enter.
    sent = False
    for getter in (
        lambda: page.get_by_role("button", name=re.compile(r"^(search|send|submit|ask)$", re.I)),
        lambda: page.locator('button[aria-label*="earch" i], button[aria-label*="end" i]'),
    ):
        try:
            b = getter()
            if b.count() > 0 and b.first.is_enabled():
                b.first.click()
                sent = True
                break
        except Exception:
            continue
    if not sent:
        try:
            box.press("Enter")
        except Exception:
            page.keyboard.press("Enter")
    _log(f"  Submitted question ({len(fitted)} chars).")
    page.wait_for_timeout(2500)
    _shot(page, debug_dir, "03_submitted")


def _cite_buttons(page):
    return page.get_by_role("button", name=re.compile(r"^Cite$", re.I))


def wait_for_results(page, debug_dir: Path | None) -> None:
    """Poll until the result cards render (a 'Found N results' banner or Cite buttons)."""
    _log("  Waiting for Scholar Labs to evaluate results...")
    start = time.time()
    while time.time() - start < SEARCH_TIMEOUT_S:
        page.wait_for_timeout(int(POLL_INTERVAL_S * 1000))
        elapsed = int(time.time() - start)
        try:
            if page.get_by_text(re.compile(r"Found\s+\d+\s+relevant result", re.I)).count() > 0:
                page.wait_for_timeout(1500)
                _log(f"  Results ready after {elapsed}s.")
                _shot(page, debug_dir, "04_results")
                return
            if _cite_buttons(page).count() > 0:
                page.wait_for_timeout(1500)
                _log(f"  Results ready after {elapsed}s (cite controls present).")
                _shot(page, debug_dir, "04_results")
                return
            no_res = page.get_by_text(
                re.compile(r"No results|couldn.t find|no relevant|did not (?:find|return)", re.I))
            still_looking = page.get_by_text(re.compile(r"Looking for results|Evaluating", re.I))
            if no_res.count() > 0 and still_looking.count() == 0:
                _log("  Scholar Labs reported no results.")
                _shot(page, debug_dir, "04_no_results")
                return
        except Exception:
            pass
        if elapsed and elapsed % 30 == 0:
            _log(f"    still evaluating... ({elapsed}s)")
    _shot(page, debug_dir, "04_timeout")
    raise TimeoutError(f"Results not ready within {SEARCH_TIMEOUT_S}s")


def _export_link_name(fmt: str) -> re.Pattern:
    return re.compile(r"^RefMan$", re.I) if fmt == "ris" else re.compile(r"^BibTeX$", re.I)


def _dismiss_open_dialog(page) -> None:
    """Best-effort close of any open Cite modal so the next card's click isn't
    intercepted by a lingering overlay."""
    for getter in (
        lambda: page.get_by_role("button", name=re.compile(r"^(cancel|close)$", re.I)),
        lambda: page.locator('[aria-label="Cancel"], [aria-label="Close"]'),
    ):
        try:
            c = getter()
            if c.count() > 0 and c.first.is_visible():
                c.first.click()
                page.wait_for_timeout(250)
                return
        except Exception:
            continue
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
    except Exception:
        pass


def _rate_limited(page) -> bool:
    """Google throttles the Cite/export operation under automation, returning a
    'can't perform the operation now / try again later' toast in place of the
    export links. Detect it so the driver can back off and report it honestly."""
    try:
        return page.get_by_text(
            re.compile(r"can.?t perform the operation|try again later", re.I)).count() > 0
    except Exception:
        return False


def collect_cite_links(page, fmt: str, debug_dir: Path | None) -> list[str]:
    """For each result, open Cite, read the export (BibTeX/RefMan) href, close.

    Two failure modes are handled. The Cite modal can be slow to render and a
    modal that fails to close would block the next card's click, so each card is
    retried with a dismiss-before/after guard and an href fallback. Separately,
    Google rate-limits the Cite/export operation under automation (a "try again
    later" toast in place of the export links); that is detected, backed off, and
    — if it persists — reported so the run defers cleanly rather than grinding
    through long timeouts. Returns a de-duplicated list of export URLs.
    """
    link_name = _export_link_name(fmt)
    href_frag = "scholar.ris" if fmt == "ris" else "scholar.bib"
    buttons = _cite_buttons(page)
    n = buttons.count()
    _log(f"  Found {n} citable results; reading export links...")
    hrefs: list[str] = []
    seen: set[str] = set()
    rate_limit_hits = 0
    for i in range(n):
        for attempt in range(3):
            try:
                _dismiss_open_dialog(page)            # clean slate before clicking
                buttons.nth(i).scroll_into_view_if_needed(timeout=5_000)
                buttons.nth(i).click()
                page.wait_for_timeout(600)
                if _rate_limited(page):
                    rate_limit_hits += 1
                    _dismiss_open_dialog(page)
                    page.wait_for_timeout(min(5_000 * (attempt + 1), 15_000))   # back off
                    if attempt == 2:
                        _log(f"    (skipped result {i + 1}: Google rate-limited the export)")
                    continue
                link = page.get_by_role("link", name=link_name)
                try:
                    link.first.wait_for(state="visible", timeout=CITE_MODAL_TIMEOUT_MS)
                    target = link.first
                except Exception:
                    # Fallback: the export anchor always carries the export href.
                    target = page.locator(f'a[href*="{href_frag}"]').first
                    target.wait_for(state="visible", timeout=4_000)
                href = target.get_attribute("href") or ""
                if href and href not in seen:
                    hrefs.append(href)
                    seen.add(href)
                _dismiss_open_dialog(page)
                break  # success — next card
            except Exception as e:
                _dismiss_open_dialog(page)
                page.wait_for_timeout(400)
                if attempt == 2:
                    _log(f"    (skipped result {i + 1} after 3 tries: {type(e).__name__})")
        # If Google keeps throttling and nothing is getting through, stop early —
        # it will not recover within this session.
        if rate_limit_hits >= 5 and not hrefs:
            _log("  Google is rate-limiting the Cite export ('try again later') — stopping "
                 "early. Retry later from a fresh session, or rely on Deep Research / Undermind.")
            break
        page.wait_for_timeout(1_000)     # pace between cards to avoid tripping the limit
    _log(f"  Collected {len(hrefs)} export links.")
    if not hrefs:
        _shot(page, debug_dir, "05_no_cites")
    return hrefs


def fetch_bibtex(context, hrefs: list[str]) -> str:
    """Fetch each export URL with the logged-in session and concatenate."""
    chunks: list[str] = []
    for href in hrefs:
        try:
            resp = context.request.get(href, timeout=30_000)
            if resp.ok:
                text = resp.text()
                if text and "@" in text:
                    chunks.append(text.strip())
        except Exception:
            continue
    return "\n\n".join(chunks) + ("\n" if chunks else "")


# ── orchestration ─────────────────────────────────────────────────────────────

def _load_query(args) -> str:
    if args.query_file:
        p = Path(args.query_file)
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace").strip()
    if args.query:
        return args.query.strip()
    if args.research_question:
        return args.research_question.strip()
    if getattr(args, "question", ""):
        return args.question.strip()
    return ""


def _defer(output: Path, message: str) -> None:
    """Graceful no-op: emit sentinel, write empty outputs, exit 0."""
    _log(f"{SENTINEL}: {message}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("[]", encoding="utf-8")
    output.with_suffix(".bib").write_text("", encoding="utf-8")
    sys.exit(0)


def run(args) -> None:
    output = Path(args.output)
    debug_dir = Path(args.debug_dir) if args.debug_dir else None
    user_data_dir = Path(args.user_data_dir).expanduser()
    hidden = bool(getattr(args, "hidden", False)) and not args.login
    headed = bool(args.headed or args.login or hidden)
    invisible = bool(getattr(args, "invisible", False)) and not headed

    query = "" if args.login else _load_query(args)
    if not args.login and not query:
        _defer(output, "No research question supplied (need --query-file/--query).")

    fmt = getattr(args, "format", "bibtex")
    ext = ".ris" if fmt == "ris" else ".bib"
    raw_export = (debug_dir or output.parent) / f"scholarlabs_export{ext}"

    # Any failure from here (Playwright import, launch, the flow) degrades to a
    # graceful defer (exit 0) so the orchestrator marks the stage "deferred".
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # Reduce automation fingerprinting so Google's sign-in does not flag
            # the window ("this browser or app may not be secure").
            base_args = [
                "--disable-blink-features=AutomationControlled",
                # Keep the off-screen / occluded window running at full speed.
                # Otherwise Chrome background-throttles its timers and rendering,
                # and the Cite modal's async content never loads within the timeout
                # (the cite-modal TimeoutError that drops Scholar Labs results).
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ]
            if invisible:
                # Chrome's "new" headless renders a full browser with a normal
                # user agent (old headless sends "HeadlessChrome", which Google
                # flags). Launch headful at the Playwright layer so it does not
                # add the old --headless switch.
                base_args.append("--headless=new")
                pw_headless = False
            else:
                pw_headless = not headed
            if hidden:
                # Real headed Chrome (passes Google's checks) rendered far
                # off-screen so it never disrupts the user.
                base_args += ["--window-position=-32000,-32000"]
            launch_kwargs = dict(user_data_dir=str(user_data_dir), headless=pw_headless,
                                 accept_downloads=True,
                                 viewport={"width": 1480, "height": 1000},
                                 args=base_args,
                                 ignore_default_args=["--enable-automation"])
            try:
                context = p.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
            except Exception:
                context = p.chromium.launch_persistent_context(**launch_kwargs)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                if args.login:
                    do_login(page, debug_dir)
                    return

                _log("Opening Google Scholar Labs...")
                page.goto(APP_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_MS)
                page.wait_for_timeout(3000)
                _shot(page, debug_dir, "00_landing")
                email, password = _get_credentials(prompt_if_missing=False)
                # An off-screen (--hidden) window cannot be used to approve 2FA,
                # so treat it like headless for the 2FA path (alert + defer).
                status = reach_app(page, email, password, debug_dir,
                                   headless=(not headed) or hidden)
                if status != "ok":
                    if status == "captcha":
                        _defer(output, "Google served an 'unusual traffic' CAPTCHA (bot "
                               "detection). Scholar Labs blocks headless/automated access; "
                               "run this stage headed (or wait and re-run --login), then retry.")
                    elif status == "2fa":
                        _desktop_alert("Google needs 2-step verification for Scholar Labs. "
                                       "Run: scholarlabs_search.py --login (headed) to authorize.")
                        _defer(output, "Login needs 2FA — re-run --login (headed) to authorize.")
                    else:
                        _defer(output, "Not signed in (or no SCHOLAR_EMAIL/PASSWORD). "
                                       "Run:  scholarlabs_search.py --login")
                _log("  Signed in.")

                submit_query(page, query, debug_dir)
                wait_for_results(page, debug_dir)
                hrefs = collect_cite_links(page, fmt, debug_dir)
                if not hrefs:
                    _defer(output, "No citable results found on the Scholar Labs page.")
                bibtext = fetch_bibtex(context, hrefs)
                if not bibtext.strip():
                    _defer(output, "Export links produced no BibTeX content.")
                raw_export.parent.mkdir(parents=True, exist_ok=True)
                raw_export.write_text(bibtext, encoding="utf-8")
                _log(f"  Wrote {raw_export}")
            except SystemExit:
                raise
            except Exception as e:
                _scrub_credentials(page)
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
        _defer(output, f"Scholar Labs driver could not start ({type(e).__name__}: {e}).")

    if args.login:
        return

    # Parse + enrich the collected BibTeX; an ingest failure also defers. The
    # ingest import lives here (not at module top) so an import-time dependency
    # failure also degrades via _defer rather than crashing without a sentinel.
    try:
        if not raw_export.is_file() or raw_export.stat().st_size < 30:
            _defer(output, "Export produced no reference file.")
        from scholarlabs_ingest import ingest
        _log("Parsing + enriching the collected citations...")
        results = ingest(raw_export, output, enrich=not args.no_enrich,
                         source="scholarlabs", sibling="bibtex")
        # A non-empty export that parses to zero records is parser/format drift,
        # not a clean empty result: defer so the orchestrator does not record
        # silent success on a populated export.
        if not results:
            _defer(output, "Non-empty export parsed to zero records (parser/format drift).")
        _log(f"Saved {len(results)} papers to {output}")
    except SystemExit:
        raise
    except Exception as e:
        _defer(output, f"Ingest of the collected citations failed ({type(e).__name__}: {e}).")


def main() -> None:
    ap = argparse.ArgumentParser(description="Google Scholar Labs deep-search driver.")
    ap.add_argument("question", nargs="?", default="", help="Question text (or use --query-file/--query)")
    ap.add_argument("--query-file", default="", help="Path to a file with the research question")
    ap.add_argument("--query", default="", help="Research question (standalone use)")
    ap.add_argument("--research-question", default="", help="Fallback question if no query/file given")
    ap.add_argument("-o", "--output", default="stage2_scholarlabs.json", help="Output JSON path")
    ap.add_argument("--debug-dir", default="", help="Directory for screenshots / HTML dumps")
    ap.add_argument("--user-data-dir", default=str(DEFAULT_PROFILE),
                    help=f"Persistent browser profile (default: {DEFAULT_PROFILE})")
    ap.add_argument("--headed", action="store_true", help="Show the browser (debugging)")
    ap.add_argument("--invisible", action="store_true",
                    help="Try Chrome's 'new' headless (no window). Note: Google still blocks Scholar this way — --headed is the reliable mode")
    ap.add_argument("--hidden", action="store_true",
                    help="Real headed Chrome positioned off-screen: passes Google's checks like --headed but stays out of sight")
    ap.add_argument("--login", action="store_true",
                    help="Headed first-run setup: capture credentials to env (prompts if missing) and sign in")
    ap.add_argument("--format", choices=("bibtex", "ris"), default="bibtex",
                    help="Export format to read from Scholar's Cite menu (default: bibtex)")
    ap.add_argument("--no-enrich", action="store_true", help="Skip Crossref/OpenAlex enrichment")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
