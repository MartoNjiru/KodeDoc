# KodeDoc — AI Debug Agent

**Version:** 2.2.0  
**Type:** Terminal AI code debugger  
**Stack:** Python 3 · Ollama (primary) · Anthropic Claude (fallback) · Rich UI

KodeDoc is a local/remote AI debug agent. You load broken code, pick a language and an Ollama model, and it runs a multi-step pipeline: deep static analysis, polyglot conflict detection & repair, dependency hints, full rewrite, and 5 independent verification passes. Output is streamed, diffable, exportable, and copyable.



---

## Table of contents

1. [Overview](#overview)
2. [Features](#features)
3. [Requirements](#requirements)
4. [Installation](#installation)
5. [Quick start](#quick-start)
6. [Configuration](#configuration)
7. [Commands](#commands)
8. [Debug pipeline](#debug-pipeline)
9. [Polyglot repair](#polyglot-repair)
10. [Supported languages](#supported-languages)
11. [Environment variables](#environment-variables)
12. [Architecture](#architecture)
13. [Examples](#examples)
14. [Limitations](#limitations)
15. [Troubleshooting](#troubleshooting)
16. [Contributing](#contributing)
17. [License](#license)
18. [Changelog](#changelog)

---

## Overview

Write one short paragraph that answers: *What is it, who is it for, and what problem does it solve?*

Suggested content:

- KodeDoc connects to an Ollama instance (local or remote) and optionally falls back to the Anthropic Claude API.
- It is aimed at developers who want a **structured, multi-pass** fix loop instead of a single “fix my code” prompt.
- It emphasizes **polyglot contamination** (e.g. `console.log` inside Python), incomplete model replies, and verification before accepting a fix.
- Local file load limit is 1 GB for convenience; model context/output limits still apply.

Keep marketing light. Lead with capability and honesty about limits.

---

## Features

Use a bullet list. Group by capability.

**Core**

- Multi-language debug pipeline (30 languages in the picker)
- Ollama primary backend + Claude HTTP fallback
- Streaming fix output
- Unified diff (optional)
- Session history (metadata only — memory safe)
- Soft size warning when buffer exceeds ~80k characters

**Polyglot**

- Static signature scan for foreign-language constructs
- Dedicated polyglot repair loop (up to 3 attempts)
- Post-repair re-scan and dedicated polyglot verify prompt
- Comment-aware skip rules to reduce false positives

**Ops / UX**

- Model picker against live `/api/tags`
- Configurable Ollama base URL (`url` command)
- Dependency detection + optional real install (`pip` / `npm` when `auto always`)
- Export to file / copy to clipboard
- `status`, `history`, `help`, `clear`, `show`

**Safety / quality**

- Unified extraction threshold (reject short/garbage model output)
- Incomplete stream / length-limit detection
- No Rich markup inside AI prompts (raw source only)

---

## Requirements

List hard and soft requirements.

| Requirement | Notes |
|-------------|--------|
| Python 3.9+ | Typed with `from __future__ import annotations` |
| `requests` | HTTP to Ollama / Claude |
| `rich` | Terminal UI |
| Ollama | Running instance with at least one model pulled |
| Optional: `ANTHROPIC_API_KEY` | Claude fallback |
| Optional: `pyperclip` / system clipboard tools | `copy` / `paste` |

OS notes:

- Clipboard: `pbcopy`/`pbpaste` (macOS), `xclip`/`xsel` (Linux)
- Editor: `$EDITOR` or `nano` for the `edit` command

---

## Installation

Give exact commands a stranger can paste.

```bash
# clone
git clone https://github.com/<you>/kodedoc.git
cd kodedoc

# deps
pip install requests rich

# optional clipboard helper
pip install pyperclip

# optional Claude SDK (HTTP fallback works without it)
# pip install anthropic
```

Ensure Ollama is installed and a model is available:

```bash
ollama serve
ollama pull codellama:7b
# or: deepseek-coder:6.7b, qwen2.5-coder:7b, etc.
```

Run:

```bash
python kodedoc.py
```

---

## Quick start

Numbered steps only — no theory.

1. Start Ollama and pull a code model.
2. Launch KodeDoc: `python kodedoc.py`
3. `model` → select an installed model  
4. `lang` → e.g. `Python`  
5. `sample` (or `load` / `paste` / `edit`)  
6. `debug`  
7. Review fixed code → `export` or `copy`

Minimal happy path:

```text
> model
> lang
> sample
> debug
> export
```

---

## Configuration

Document runtime knobs that are not environment variables.

| Setting | How to change | Default |
|---------|---------------|---------|
| Ollama URL | `url` command | `http://localhost:11434` |
| Model | `model` command | none |
| Language | `lang` command | none |
| Diff view | `diff` toggle | OFF |
| Auto-install deps | `auto` cycle: ask → always → never | ask |

Note clearly:

- Local load ceiling is **1 GB**.
- A 1 GB file is **not** a promise that the model can process the whole file in one request. Context and generation limits still apply.

---

## Commands

Reproduce the in-app help table. Keep descriptions short.

| Command | Description |
|---------|-------------|
| `model` | Pick a model from your Ollama instance |
| `lang` | Select a programming language (30 supported) |
| `url` | Set Ollama base URL |
| `load` | Load a local file (up to 1 GB) |
| `paste` | Paste code from clipboard |
| `edit` | Open `$EDITOR` to write/paste code |
| `sample` | Load a buggy example for the selected language |
| `debug` | Full pipeline: analysis → polyglot repair → fix → 5× verify |
| `copy` | Copy fixed code to clipboard |
| `export` | Save fixed code to a file |
| `diff` | Toggle unified diff view |
| `auto` | Cycle auto-install: ask → always → never |
| `clear` | Reset the code buffer |
| `show` | Display current buffer with syntax highlighting |
| `history` | Show past debug sessions (metadata) |
| `status` | Full status dump |
| `help` | Command reference |
| `quit` / `exit` | Exit |

---

## Debug pipeline

Describe the ordered steps so reviewers understand the design.

1. **Deep analysis** — exhaustive checklist (syntax, names, imports, logic, types, polyglot, deps). Numbered issue list from the model.
2. **Static polyglot scan** — regex signatures for foreign constructs in the selected language; comment lines skipped where practical.
3. **Dependency scan** — language-aware import/require/include mining + analysis text; optional install.
4. **Polyglot repair loop** — up to 3 attempts: targeted fix prompt → extract → re-scan → optional dedicated polyglot verify.
5. **Full rewrite** — single fix pass using the analysis + working code (streaming).
6. **5× verification** — independent passes; `VERIFIED OK` or `ISSUE:` + corrected code extraction.
7. **Post-fix polyglot scan** — final static check on the accepted output.
8. **Display / export** — syntax panel, optional unified diff, session metadata log.

Call out the extraction guard: responses that are empty, single-line, or far shorter than the original are rejected (`EXTRACTION_MIN_RATIO` / equivalent).

---

## Polyglot repair

Dedicated subsection — this is a differentiator.

- **Detection:** `POLYGLOT_SIGNATURES` map primary language → list of `(secondary_lang, regex)`.
- **Display vs prompt:** Rich `escape()` is used only for terminal display. Prompts always receive **raw** source so brackets/braces are not corrupted.
- **Repair:** `build_polyglot_fix_prompt` lists conflict lines with language tags and translation references.
- **Verify:** `build_polyglot_verify_prompt` expects `POLYGLOT CLEAN` or listed issues + corrected code.
- **Loop:** Max attempts configurable (`MAX_POLY_REPAIR_ATTEMPTS`); remaining conflicts are passed into the general fix step.

Example intentional sample (`Python_polyglot` / sample under Python): JS `console.log` / `var` and Java `System.out.println` inside Python.

---

## Supported languages

List the picker languages as a compact paragraph or table.

Python, JavaScript, TypeScript, C, C++, C#, Java, Go, Rust, PHP, Ruby, Swift, Kotlin, Dart, R, Scala, Lua, Perl, Shell, PowerShell, SQL, HTML, CSS, YAML, JSON, XML, Markdown, Assembly, MATLAB, Julia.

Extension → language auto-detect is available on `load` for common extensions (`.py`, `.js`, `.ts`, `.go`, `.rs`, etc.).

---

## Environment variables

Document every env var the code reads. (If your tree includes the token-budget helpers from later patches, list them; if not, say so and keep this section ready for when you add them.)

| Variable | Purpose | Default (if present) |
|----------|---------|----------------------|
| `ANTHROPIC_API_KEY` | Claude API fallback | unset (fallback disabled) |
| `EDITOR` | Editor for `edit` command | `nano` |
| `KODEDOC_CONTEXT_TOKENS` | Ollama `num_ctx` (if implemented) | `65536` |
| `KODEDOC_GENERATION_TOKENS` | Ollama `num_predict` (if implemented) | `32768` |
| `KODEDOC_CLAUDE_MAX_TOKENS` | Claude `max_tokens` (if implemented) | `8192` |

Example (PowerShell / bash depending on your docs audience):

```bash
export KODEDOC_CONTEXT_TOKENS=65536
export KODEDOC_GENERATION_TOKENS=32768
export ANTHROPIC_API_KEY=sk-ant-...
python kodedoc.py
```

State clearly: larger budgets only help if the **model and server** support them.

---

## Architecture

High-level map for contributors — not a full design doc.

```
kodedoc.py
├── State                 # model, url, lang, last_fixed, session_log, flags
├── Ollama / Claude API   # call_ollama, _stream_ollama, call_claude_api, call_model
├── Polyglot              # detect_polyglot_conflicts, repair_polyglot_conflicts
├── Prompts               # analysis, polyglot fix, fix, polyglot verify, verify
├── Utils                 # extract_code, is_valid_extraction, deps, diff
├── UI flows              # model, lang, url, load, editor, clipboard
├── Pipeline              # run_debug
└── REPL                  # main()
```

Design rules worth stating in the README:

- Composition over deep inheritance; pure-ish edges, side effects at the boundary.
- Errors from the model are values (`None` / rejected extraction), not silent success.
- Session log stores **metadata + short samples**, not full source of large files.

---

## Examples

Show 2–3 concrete flows.

**Sample Python debug**

```text
> lang
Language: Python
> sample
> model
> debug
```

**Load a real file**

```text
> lang
> load
File path: ~/projects/broken_script.py
> debug
> export
Save as: fixed_script.py
```

**Remote Ollama**

```text
> url
New URL: https://ollama.example.com
> model
> debug
```

Optional: paste a before/after snippet from the intentional samples so visitors see polyglot repair in action.

---

## Limitations

Be explicit — builds trust.

- Model quality dominates results; KodeDoc orchestrates, it does not guarantee correct code.
- Large files may exceed model context; the 1 GB load limit is local I/O only.
- Polyglot detection is signature-based; novel foreign constructs can slip through.
- Dependency install is only “real” for Python (`pip`) and JS/TS (`npm`) when auto-install is enabled; other languages are recorded/simulated.
- Claude fallback requires a valid API key and network access.
- Verification is model-based; majority “OK” is a heuristic, not a formal proof.

---

## Troubleshooting

| Symptom | Likely cause | What to try |
|---------|--------------|-------------|
| Cannot connect to Ollama | Service down / wrong URL | `ollama serve`, `url` command, firewall |
| No models listed | Nothing pulled | `ollama pull codellama:7b` |
| Analysis / fix returns empty | Model refuse / timeout / length | Smaller buffer, larger model, raise token budgets if supported |
| Extraction rejected | Model returned summary instead of full code | Re-run; try a code-specialist model |
| Polyglot still present after fix | Weak model / residual signatures | Manual review; extra repair pass |
| Clipboard fails | No `pbcopy`/`xclip`/`xsel`/`pyperclip` | Use `export` |
| Claude never used | Missing key | Set `ANTHROPIC_API_KEY` |

---

## Contributing

Short policy only.

1. Open an issue for bugs / feature ideas before large PRs.
2. Keep the single-file layout unless the change clearly needs a package split.
3. Do not put Rich markup into AI prompt builders.
4. Prefer tighter polyglot patterns over broader ones (false positives are costly).
5. Run a local smoke path: `lang` → `sample` → `debug` with a small code model.
6. Update this README’s Commands / Changelog when user-facing behavior changes.

Suggested issue labels: `bug`, `polyglot`, `ux`, `docs`, `deps`.

---

## License

Pick one and state it clearly, e.g.:

```text
MIT License — see LICENSE
```

If you have not chosen yet, say “License TBD” until you add a `LICENSE` file. Do not claim a license you have not published.

---

## Changelog

Mirror the version history that already lives in the module docstring, expanded for GitHub.

### v2.2.0

- Tightened polyglot signatures; PHP `$var` no longer false-positives on Python.
- Unified extraction threshold.
- Claude model id corrected to a stable public identifier.
- Real optional install path for `pip` / `npm` under `auto always`.
- Session log memory-safe (metadata + short samples).
- Added `status` and `url` commands.
- Cleaner timeout / connection messages.
- Soft warning for large buffers.

### v2.1.0 (summary)

- Polyglot repair loop, post-repair re-scan, dedicated polyglot verify.
- Expanded polyglot signatures.
- Extraction threshold raised; escape() never used inside AI prompts.
- 5× verify, streaming, dependency extraction, 1 GB local load.

### Earlier

- Initial Ollama + Claude debug agent, multi-language samples, REPL.

Keep newest version at the top. One bullet per user-visible change.

---

## Suggested GitHub extras (optional)

Not required for a minimal repo, but useful:

| File / item | Purpose |
|-------------|---------|
| `LICENSE` | SPDX license text |
| `requirements.txt` | `requests`, `rich` (pin if you care about reproducibility) |
| `.gitignore` | `__pycache__/`, `.venv/`, `fixed_code.*`, editor junk |
| `CONTRIBUTING.md` | Expand the Contributing section if the project grows |
| GitHub Topics | `ollama`, `code-debugging`, `python`, `rich`, `ai-tools` |
| Repo description | One line: “Terminal AI debug agent with Ollama + polyglot repair and multi-pass verification” |

`.gitignore` starter:

```gitignore
__pycache__/
*.py[cod]
.venv/
venv/
.env
fixed_code.*
.DS_Store
*.log
```

`requirements.txt` starter:

```text
requests>=2.28
rich>=13.0
```

---


