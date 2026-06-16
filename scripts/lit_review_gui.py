#!/usr/bin/env python3
"""Lit-Review Orchestrator -- interactive settings dialog (the Stage 0 front door).

Claude Code launches this when the lit-review-orchestrator skill is invoked. It
collects the input document (or a raw query) and the run options, then hands the
choices back to the Claude Code session as JSON -- written to ``--config-out`` and
echoed to stdout between sentinel markers. It runs nothing and calls no API;
Claude then drives the agent-driven pipeline honouring these settings.

Exit codes: 0 = Run (config emitted), 2 = Cancel / window closed (abort the run).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CONFIG_BEGIN = "===LITREVIEW_CONFIG_BEGIN==="
CONFIG_END = "===LITREVIEW_CONFIG_END==="

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except Exception as e:  # pragma: no cover - headless environments
    sys.stderr.write(f"Tkinter is required for the lit-review GUI: {e}\n")
    sys.exit(3)

# key, label, description, default-checked
CHANNELS = [
    ("undermind", "Undermind", "Stage 1 · semantic deep search", True),
    ("deepresearch", "Deep Research", "Stage 2b · Gemini", True),
    ("scholar", "Google Scholar", "Stage 4a · SearchAPI", True),
    ("scholarlabs", "Scholar Labs", "Stage 2 · opt-in, rate-limited", False),
]
# Keyless channels — no API key or login needed (the "only Claude Code" path).
# Both default on: free index search is a fast keyless subprocess, and web search
# uses the agent's own WebSearch/WebFetch tools for broad open-web coverage.
# key, label, description, default-checked
KEYLESS_CHANNELS = [
    ("freesearch", "Free index search", "Stage 4e · OpenAlex/Crossref/S2", True),
    ("websearch", "Web search", "Stage 4d · agent WebSearch", True),
]
# key, label, default-checked. SSRN defaults on (targets the real ssrn.com repo).
# "forthcoming" is intentionally absent: it is not a source, just a Google Scholar
# source: filter for three finance journals (JF/JFE/RFS); the --forthcoming CLI flag
# still exists for anyone who wants it.
SUPP = [("ssrn", "SSRN", True), ("nber", "NBER", False), ("heinonline", "HeinOnline", False)]
DEEP_CHANNELS = ("undermind", "deepresearch", "scholarlabs")  # greyed by Quick mode


class App(tk.Tk):
    def __init__(self, config_out: str | None):
        super().__init__()
        self.config_out = config_out
        self.result_written = False
        self.vars: dict[str, tk.BooleanVar] = {}
        self.channel_cbs: dict[str, ttk.Checkbutton] = {}
        self.supp_cbs: list[ttk.Checkbutton] = []
        self.title("Lit-Review Orchestrator")
        self.minsize(680, 560)
        self._build()
        self._sync_states()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        # Pop to front when launched by Claude Code (window can otherwise open behind).
        self.lift()
        self.attributes("-topmost", True)
        self.after(600, lambda: self.attributes("-topmost", False))

    # -- construction --------------------------------------------------------
    def _build(self):
        pad = dict(padx=8, pady=4)
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True, padx=10, pady=10)

        ttk.Label(root, text="Settings are returned to the Claude Code session, which then "
                  "runs the agent-driven pipeline.", foreground="#555").pack(anchor="w", padx=2, pady=(0, 6))

        # Input — Document, then Output dir, then Raw query (optional) at the bottom.
        inp = ttk.LabelFrame(root, text="Input")
        inp.pack(fill="x", **pad)
        self.var_doc = tk.StringVar()
        self.var_query = tk.StringVar()
        self.var_out = tk.StringVar()
        self._row_browse(inp, "Document", self.var_doc, self._browse_doc, 0)
        self._row_browse(inp, "Output dir", self.var_out, self._browse_out, 1)
        ttk.Label(inp, text="Raw query").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(inp, textvariable=self.var_query).grid(row=2, column=1, sticky="ew", padx=6, pady=4)
        ttk.Label(inp, text="(optional — skips Stage 0)", foreground="#666").grid(
            row=2, column=2, sticky="w", padx=6)
        inp.columnconfigure(1, weight=1)

        # Channels — keyed (need an API key / login) on the left, keyless on the right.
        ch = ttk.LabelFrame(root, text="Search channels")
        ch.pack(fill="x", **pad)
        keyed = ttk.LabelFrame(ch, text="Keyed — need API key / login")
        keyed.grid(row=0, column=0, sticky="nsew", padx=(6, 4), pady=4)
        keyless = ttk.LabelFrame(ch, text="Keyless — no key needed")
        keyless.grid(row=0, column=1, sticky="nsew", padx=(4, 6), pady=4)
        ch.columnconfigure(0, weight=1)
        ch.columnconfigure(1, weight=1)
        for i, (key, label, desc, default) in enumerate(CHANNELS):
            v = tk.BooleanVar(value=default)
            self.vars[key] = v
            cb = ttk.Checkbutton(keyed, text=label, variable=v, command=self._on_change)
            cb.grid(row=i, column=0, sticky="w", padx=6, pady=2)
            self.channel_cbs[key] = cb
            ttk.Label(keyed, text=desc, foreground="#666").grid(row=i, column=1, sticky="w", padx=6)
        for i, (key, label, desc, default) in enumerate(KEYLESS_CHANNELS):
            v = tk.BooleanVar(value=default)
            self.vars[key] = v
            cb = ttk.Checkbutton(keyless, text=label, variable=v, command=self._on_change)
            cb.grid(row=i, column=0, sticky="w", padx=6, pady=2)
            self.channel_cbs[key] = cb
            ttk.Label(keyless, text=desc, foreground="#666").grid(row=i, column=1, sticky="w", padx=6)

        # Supplementary
        sp = ttk.LabelFrame(root, text="Supplementary sources")
        sp.pack(fill="x", **pad)
        for i, (key, label, default) in enumerate(SUPP):
            v = tk.BooleanVar(value=default)
            self.vars[key] = v
            cb = ttk.Checkbutton(sp, text=label, variable=v, command=self._on_change)
            cb.grid(row=0, column=i, sticky="w", padx=6, pady=2)
            self.supp_cbs.append(cb)
        self.vars["citation_chain"] = tk.BooleanVar(value=False)
        self.cc_cb = ttk.Checkbutton(sp, text="Citation chaining (Semantic Scholar)",
                                     variable=self.vars["citation_chain"], command=self._on_change)
        self.cc_cb.grid(row=1, column=0, columnspan=3, sticky="w", padx=6, pady=2)
        ttk.Label(sp, text="seeds").grid(row=1, column=3, sticky="e")
        self.var_seeds = tk.IntVar(value=20)
        self.spin_seeds = ttk.Spinbox(sp, from_=1, to=200, width=6, textvariable=self.var_seeds)
        self.spin_seeds.grid(row=1, column=4, sticky="w", padx=4)

        # Processing
        pr = ttk.LabelFrame(root, text="Processing")
        pr.pack(fill="x", **pad)
        self.vars["dedup"] = tk.BooleanVar(value=True)
        self.vars["verify"] = tk.BooleanVar(value=True)
        self.vars["screen"] = tk.BooleanVar(value=True)
        self.vars["no_llm"] = tk.BooleanVar(value=False)
        self.cb_dedup = ttk.Checkbutton(pr, text="Deduplicate", variable=self.vars["dedup"], command=self._on_change)
        self.cb_dedup.grid(row=0, column=0, sticky="w", padx=6, pady=2)
        self.cb_verify = ttk.Checkbutton(pr, text="Verify sources  (drop papers no index can confirm)",
                                         variable=self.vars["verify"], command=self._on_change)
        self.cb_verify.grid(row=1, column=0, columnspan=3, sticky="w", padx=6, pady=2)
        self.cb_screen = ttk.Checkbutton(pr, text="Screen", variable=self.vars["screen"], command=self._on_change)
        self.cb_screen.grid(row=2, column=0, sticky="w", padx=6, pady=2)
        self.cb_nollm = ttk.Checkbutton(pr, text="DOI-only (no LLM)", variable=self.vars["no_llm"], command=self._on_change)
        self.cb_nollm.grid(row=2, column=1, sticky="w", padx=6, pady=2)

        # Advanced
        adv = ttk.LabelFrame(root, text="Advanced")
        adv.pack(fill="x", **pad)
        self.vars["quick"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(adv, text="Quick (Google Scholar only)", variable=self.vars["quick"],
                        command=self._on_change).grid(row=0, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(adv, text="Max chars").grid(row=0, column=1, sticky="e", padx=6)
        self.var_maxchars = tk.IntVar(value=30000)
        ttk.Spinbox(adv, from_=2000, to=200000, increment=1000, width=8,
                    textvariable=self.var_maxchars).grid(row=0, column=2, sticky="w")
        # Model routing: off = Opus orchestrates / runs the keyless web search / re-ranks,
        # Sonnet subagents do the rest; on = every subagent runs on Opus. Both stay keyless.
        self.vars["all_opus"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            adv, text="Use Opus for all tasks  (default: Opus orchestrates / web search / re-ranks, Sonnet does the rest)",
            variable=self.vars["all_opus"], command=self._on_change,
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=6, pady=2)

        # Buttons
        btn = ttk.Frame(root)
        btn.pack(fill="x", **pad)
        ttk.Button(btn, text="Run", command=self._run).pack(side="right", padx=4)
        ttk.Button(btn, text="Cancel", command=self._cancel).pack(side="right", padx=4)

    def _row_browse(self, parent, label, var, cmd, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(parent, text="Browse…", command=cmd).grid(row=row, column=2, sticky="w", padx=6, pady=4)

    # -- browse --------------------------------------------------------------
    def _browse_doc(self):
        f = filedialog.askopenfilename(
            title="Select document",
            filetypes=[("Documents", "*.tex *.docx"), ("LaTeX", "*.tex"),
                       ("Word", "*.docx"), ("All files", "*.*")])
        if f:
            self.var_doc.set(f)
            # Default the output folder to the document's own folder.
            if not self.var_out.get().strip():
                self.var_out.set(str(Path(f).expanduser().resolve().parent))

    def _browse_out(self):
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            self.var_out.set(d)

    # -- dynamic state -------------------------------------------------------
    def _on_change(self):
        self._sync_states()

    def _sync_states(self):
        quick = self.vars["quick"].get()
        # Quick mode = Google Scholar only: grey the deep channels and the
        # supplementary / citation options so the run can't contradict itself.
        for key in DEEP_CHANNELS:
            self.channel_cbs[key].state(["disabled"] if quick else ["!disabled"])
        # Keyless channels are also off in Quick (Google Scholar only) mode.
        for key, _, _, _ in KEYLESS_CHANNELS:
            self.channel_cbs[key].state(["disabled"] if quick else ["!disabled"])
        if quick:
            self.vars["scholar"].set(True)
        for cb in self.supp_cbs:
            cb.state(["disabled"] if quick else ["!disabled"])
        self.cc_cb.state(["disabled"] if quick else ["!disabled"])
        cite_on = self.vars["citation_chain"].get() and not quick
        self.spin_seeds.configure(state=("normal" if cite_on else "disabled"))
        dedup = self.vars["dedup"].get()
        for w in (self.cb_verify, self.cb_screen, self.cb_nollm):
            w.state(["!disabled"] if dedup else ["disabled"])

    # -- config --------------------------------------------------------------
    def _config(self) -> dict:
        quick = self.vars["quick"].get()
        channels = {k: bool(self.vars[k].get()) for k, _, _, _ in CHANNELS + KEYLESS_CHANNELS}
        supp = {k: bool(self.vars[k].get()) for k, _, _ in SUPP}
        citation = bool(self.vars["citation_chain"].get())
        if quick:
            # Google Scholar only — nothing else runs.
            channels = {k: False for k in channels}
            channels["scholar"] = True
            supp = {k: False for k, _, _ in SUPP}
            citation = False
        dedup = bool(self.vars["dedup"].get())
        try:
            seeds = int(self.var_seeds.get())
        except Exception:
            seeds = 20
        try:
            maxchars = int(self.var_maxchars.get())
        except Exception:
            maxchars = 30000
        return {
            "document": self.var_doc.get().strip(),
            "query": self.var_query.get().strip(),
            "output_dir": self.var_out.get().strip(),
            "channels": channels,
            "supplementary": supp,
            "citation_chain": citation,
            "top_seeds": seeds,
            "dedup": dedup,
            "verify": bool(self.vars["verify"].get()) and dedup,
            "screen": bool(self.vars["screen"].get()) and dedup,
            "no_llm": bool(self.vars["no_llm"].get()) and dedup,
            "quick": quick,
            "max_chars": maxchars,
            "all_opus": bool(self.vars["all_opus"].get()),
        }

    # -- actions -------------------------------------------------------------
    def _run(self):
        cfg = self._config()
        doc, query = cfg["document"], cfg["query"]
        if bool(doc) == bool(query):
            messagebox.showerror("Input required",
                                 "Provide either a document or a raw query (exactly one).")
            return
        if doc and not Path(doc).expanduser().is_file():
            messagebox.showerror("File not found", f"Document does not exist:\n{doc}")
            return
        if not cfg["output_dir"]:
            messagebox.showerror("Output folder required", "Choose an output folder.")
            return
        if not any(cfg["channels"].values()):
            messagebox.showerror("No channels", "Select at least one search channel.")
            return
        if not self._emit(cfg):
            return  # write failed — keep the dialog open so the user can fix the path
        self.result_written = True
        self.destroy()

    def _emit(self, cfg: dict) -> bool:
        blob = json.dumps(cfg, indent=2)
        if self.config_out:
            try:
                p = Path(self.config_out)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(blob, encoding="utf-8")
            except Exception as e:
                messagebox.showerror("Cannot write config",
                                     f"Failed to write config to:\n{self.config_out}\n\n{e}")
                return False
        sys.stdout.write(f"\n{CONFIG_BEGIN}\n{blob}\n{CONFIG_END}\n")
        sys.stdout.flush()
        return True

    def _cancel(self):
        self.destroy()


def main():
    ap = argparse.ArgumentParser(description="Lit-Review Orchestrator settings GUI")
    ap.add_argument("--config-out", default="", help="Path to write the chosen config JSON")
    ap.add_argument("--document", default="", help="Pre-fill the document path")
    args = ap.parse_args()
    app = App(args.config_out or None)
    if args.document:
        app.var_doc.set(args.document)
        if not app.var_out.get().strip():
            app.var_out.set(str(Path(args.document).expanduser().resolve().parent))
    app.mainloop()
    sys.exit(0 if app.result_written else 2)


if __name__ == "__main__":
    main()
