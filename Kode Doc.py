"""
KodeDoc — AI Debug Agent v3.0.0 (Autonomous Edition)
"""

from __future__ import annotations

import os
import sys
import re
import json
import time
import difflib
import tempfile
import datetime
import subprocess
import shutil
from pathlib import Path
from typing import Optional, Any, List, Dict, Tuple

# --- Auto Install Deps ---
_REQUIRED = ["requests", "rich", "pyperclip"]
for pkg in _REQUIRED:
    try: 
        __import__(pkg)
    except ImportError:
        print(f"[*] Installing required package: {pkg}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

import requests
import pyperclip

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.rule import Rule
from rich import box
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.markup import escape

CONSOLE = Console()
VERSION = "3.0.0"

# --- Configuration ---
MAX_LOAD_BYTES = 1 * 1024 * 1024 * 1024
MAX_GENERATION_TOKENS = 32768
MODEL_CONTEXT_TOKENS = 65536
CLAUDE_MAX_TOKENS = 8192
VERIFY_PASSES = 3
MAX_AUTO_REPAIR_ATTEMPTS = 3
SOFT_CHAR_WARN = 80_000

LANGUAGES = [
    "Python", "JavaScript", "TypeScript", "C", "C++", "C#", "Java",
    "Go", "Rust", "PHP", "Ruby", "Swift", "Kotlin", "Dart", "R",
    "Scala", "Lua", "Perl", "Shell", "PowerShell", "SQL", "HTML",
    "CSS", "YAML", "JSON", "XML", "Markdown", "Assembly", "MATLAB", "Julia",
]

DEP_MANAGERS = {
    "Python":     {"cmd": "pip install",                    "ext": ".py", "runner": "python"},
    "JavaScript": {"cmd": "npm install",                    "ext": ".js", "runner": "node"},
    "TypeScript": {"cmd": "npm install",                    "ext": ".ts", "runner": "ts-node"},
    "Ruby":       {"cmd": "gem install",                    "ext": ".rb", "runner": "ruby"},
    "PHP":        {"cmd": "composer require",               "ext": ".php", "runner": "php"},
    "Go":         {"cmd": "go get",                         "ext": ".go", "runner": "go run"},
    "Rust":       {"cmd": "cargo add",                      "ext": ".rs", "runner": "cargo run"},
    "Shell":      {"cmd": "apt install",                    "ext": ".sh", "runner": "bash"},
}

EXT_TO_LANG = {v["ext"]: k for k, v in DEP_MANAGERS.items()}
EXT_TO_LANG.update({".html": "HTML", ".css": "CSS", ".json": "JSON", ".md": "Markdown"})

PYTHON_STDLIB = {
    "os", "sys", "re", "io", "math", "time", "json", "csv", "copy", "abc", "ast",
    "logging", "pathlib", "pickle", "platform", "pprint", "queue", "random",
    "shutil", "signal", "socket", "sqlite3", "stat", "struct", "subprocess",
    "tempfile", "threading", "traceback", "typing", "uuid", "hashlib",
    "datetime", "collections", "functools", "itertools", "contextlib", "enum",
    "string", "textwrap", "fnmatch", "glob", "html", "http", "email",
    "smtplib", "ftplib", "decimal", "dataclasses", "asyncio", "argparse",
    "configparser", "shlex", "select", "ctypes", "ssl", "urllib"
}

class State:
    def __init__(self) -> None:
        self.model: Optional[str] = None
        self.ollama_url: str = "http://localhost:11434"
        self.lang: Optional[str] = None
        self.last_fixed: str = ""
        self.diff_mode: bool = False
        self.auto_install: str = "always"
        self.session_log: list[dict[str, Any]] = []
        self.running: bool = False
        self.chat_history: list[dict[str, str]] = []

STATE = State()

def _numbered(code: str) -> str:
    return "\n".join(f"{i+1:5d} | {line}" for i, line in enumerate(code.splitlines()))

def _get_comment_marker(lang: str) -> str:
    hash_langs = ("Python", "Ruby", "Shell", "PowerShell", "Perl", "R", "Julia", "YAML")
    dash_langs = ("SQL", "Lua")
    html_langs = ("HTML", "XML", "Markdown")
    if lang in hash_langs: return "#"
    if lang in dash_langs: return "--"
    if lang in html_langs: return "<!--"
    return "//"

def build_analysis_prompt(code: str) -> str:
    marker = _get_comment_marker(STATE.lang or "")
    end = " -->" if marker == "<!--" else ""
    forced_code = f"{marker} [SYSTEM: THIS FILE IS WRITTEN IN {(STATE.lang or '').upper()}. DO NOT RE-CLASSIFY]{end}\n{code}"
    n = _numbered(forced_code)
    return (
        f"You are a world-class {STATE.lang} engineer.\n"
        f"CRITICAL LANGUAGE LOCK: The source is {STATE.lang}. Even if you see foreign syntax, treat it as polyglot contamination to fix, not a language change.\n\n"
        f"TASK: Perform an exhaustive static analysis.\n"
        f"Find EVERY defect: syntax, typos, logic, imports, missing deps, and polyglot conflicts.\n"
        f"OUTPUT FORMAT: One numbered line per issue.\n"
        f"Code:\n{n}\n\nList ALL issues now."
    )

def build_fix_prompt(code: str, analysis: str) -> str:
    marker = _get_comment_marker(STATE.lang or "")
    end = " -->" if marker == "<!--" else ""
    forced_code = f"{marker} [SYSTEM: THIS FILE IS WRITTEN IN {(STATE.lang or '').upper()}. DO NOT RE-CLASSIFY]{end}\n{code}"
    n = _numbered(forced_code)
    line_count = len(code.splitlines())
    return (
        f"You are a world-class {STATE.lang} engineer.\n"
        f"CRITICAL LANGUAGE LOCK: Output must be pure {STATE.lang}.\n\n"
        f"TASK: Rewrite the code so EVERY defect in the analysis is fixed.\n"
        f"KNOWN ISSUES:\n{analysis}\n\n"
        f"STRICT RULES:\n"
        f"  - Output ONLY the raw fixed code. No markdown backticks, no commentary.\n"
        f"  - If the original has {line_count} lines, your output MUST have at least {line_count} lines.\n"
        f"  - Translate all foreign syntax into correct {STATE.lang}.\n\n"
        f"Original code:\n{n}\n\nFixed {STATE.lang} code:"
    )

def build_test_prompt(code: str) -> str:
    return (
        f"You are a senior {STATE.lang} QA engineer.\n\n"
        f"TASK: Write a comprehensive set of unit tests for the following code.\n"
        f"RULES:\n"
        f"  - Use the standard testing framework for {STATE.lang} (e.g., pytest for Python, jest for JS).\n"
        f"  - Cover edge cases and expected logic.\n"
        f"  - Output ONLY the raw test code. No markdown backticks, no commentary.\n\n"
        f"Code to test:\n{_numbered(code)}\n\nTest code:"
    )

def build_convert_prompt(code: str, target_lang: str) -> str:
    return (
        f"You are a world-class software engineer.\n\n"
        f"TASK: Convert the following {STATE.lang} code into {target_lang}.\n"
        f"RULES: Use idiomatic {target_lang}. Output ONLY raw code. No markdown.\n\n"
        f"Source code:\n{_numbered(code)}\n\nConverted {target_lang} code:"
    )

def call_ollama(prompt: str, stream: bool = False) -> Optional[str]:
    if not STATE.model: return None
    url = f"{STATE.ollama_url.rstrip('/')}/api/generate"
    
    # FIX 1: Truncate massive prompts so the model doesn't choke
    if len(prompt) > 25000:
        CONSOLE.print("  [yellow]⚠ Prompt is too large. Truncating to fit context window.[/yellow]")
        prompt = prompt[:25000] + "\n... [TRUNCATED]"

    payload = {
        "model": STATE.model, 
        "prompt": prompt, 
        "stream": stream,
        "options": {
            "temperature": 0.0, 
            "num_predict": 4096,   # Max 4k output tokens (faster)
            "top_p": 0.9, 
            "num_ctx": 4096        # Max 4k context window
        },
    }
    
    try:
        if stream:
            full = ""
            # FIX 2: Timeout to 5 mins so it doesn't hang forever
            with requests.post(url, json=payload, stream=True, timeout=300) as resp:
                resp.raise_for_status()
                for raw_line in resp.iter_lines():
                    if raw_line:
                        try:
                            obj = json.loads(raw_line)
                            chunk = obj.get("response", "")
                            full += chunk
                            # FIX 3: Use CONSOLE.print so Rich handles the terminal buffer
                            CONSOLE.print(chunk, end="", markup=False, highlight=False)
                        except json.JSONDecodeError: pass
            CONSOLE.print()
            return full or None
            
        resp = requests.post(url, json=payload, timeout=300)
        resp.raise_for_status()
        return resp.json().get("response", "") or None
        
    except requests.exceptions.Timeout:
        CONSOLE.print("\n  [red]✖ Model timed out after 5 minutes.[/red]")
        return None
    except requests.exceptions.HTTPError as e:
        CONSOLE.print(f"\n  [red]✖ Model rejected the request: {e}[/red]")
        return None
    except Exception as exc:
        CONSOLE.print(f"\n  [red]✖ Ollama error: {escape(str(exc))}[/red]")
        return None

def call_model(prompt: str, stream: bool = False) -> Optional[str]:
    return call_ollama(prompt, stream=stream)

def extract_code(text: str) -> str:
    """Strip markdown fences, refusals, and AI placeholders."""
    
    # Catch AI refusals or giving up
    refusal_pattern = r"(?i)^(?:sorry[,! ]*)?(?:this|the provided|it) (?:code|script|snippet|file) (?:is|appears to be|looks like|seems to be) (?:not|written in|a|an) [a-zA-Z0-9_+#.]+"
    if re.search(refusal_pattern, text[:300]):
        text = re.sub(refusal_pattern, "Here is the fixed code:\n", text)

    match = re.search(r"```(?:[a-zA-Z0-9_+#.-]*)?\n?([\s\S]*?)```", text)
    if match:
        code = match.group(1).strip()
    else:
        lines = text.strip().splitlines()
        if lines and re.match(r"^(Fixed|Corrected|Rewritten|Output|Result|Fully corrected|Complete|Here|Test)\s", lines[0], re.I):
            lines = lines[1:]
        code = "\n".join(lines).strip()

    # SANITY CHECK: Catch placeholders and AI giving up
    bad_signals = [
        "Placeholder", 
        "Please provide", 
        "Error context",
        "I cannot", 
        "I need the full context",
        "// ... rest of code",
        "# ... rest of code"
    ]
    for signal in bad_signals:
        if signal.lower() in code.lower():
            CONSOLE.print(f"  [red]✖ AI hallucinated a placeholder instead of fixing the code.[/red]")
            return "" # Return empty so the pipeline knows it failed
            
    return code

def extract_dependencies(analysis_text: str, code: str) -> list[str]:
    deps = set()
    if STATE.lang == "Python":
        for imp in re.findall(r"^(?:import|from)\s+([\w.]+)", code, re.MULTILINE):
            top = imp.split(".")[0]
            if top and top not in PYTHON_STDLIB and not top.startswith("_"):
                deps.add(top)
    pkg_mentions = re.findall(r"(?:install|missing|import|require|include|add)\s+(?:the\s+)?[`'\"]?([a-zA-Z][a-zA-Z0-9_\-\.]+)[`'\"]?", analysis_text, re.IGNORECASE)
    for p in pkg_mentions:
        if 2 < len(p) < 60: deps.add(p)
    return list(deps)

def run_install(deps: list[str]) -> None:
    if not deps or STATE.lang not in DEP_MANAGERS: return
    real = True
    if STATE.lang == "Python": base_cmd = [sys.executable, "-m", "pip", "install"]
    elif STATE.lang in ("JavaScript", "TypeScript"): base_cmd = ["npm", "install"]
    else: real = False

    if real:
        for dep in deps:
            CONSOLE.print(f"  [dim]$ {' '.join(base_cmd)} {dep}[/dim]")
            try:
                subprocess.run(base_cmd + [dep], capture_output=True, text=True, timeout=120)
                CONSOLE.print(f"  [green]✔ {dep} installed[/green]")
            except Exception as e:
                CONSOLE.print(f"  [yellow]⚠ {dep}: {e}[/yellow]")

def execute_and_fix(code: str) -> str:
    """Writes code to disk, runs it, catches errors, and auto-fixes."""
    runner = DEP_MANAGERS.get(STATE.lang or "", {}).get("runner")
    if not runner: return code
    
    ext = DEP_MANAGERS.get(STATE.lang or "", {}).get("ext", ".txt")
    tmp_path = f"kodedoc_tmp{ext}"
    Path(tmp_path).write_text(code, encoding="utf-8")
    
    CONSOLE.print(f"\n[bold cyan]● Agentic Execution — Testing via `{runner} {tmp_path}`…[/bold cyan]")
    
    for attempt in range(1, MAX_AUTO_REPAIR_ATTEMPTS + 1):
        CONSOLE.print(f"  [dim]Attempt {attempt}/{MAX_AUTO_REPAIR_ATTEMPTS}...[/dim]")
        try:
            res = subprocess.run([runner, tmp_path], capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                CONSOLE.print("  [green]✔ Code executed successfully![/green]")
                if res.stdout: CONSOLE.print(f"  [dim]{res.stdout[:500]}[/dim]")
                break
            else:
                error_msg = res.stderr.strip() or res.stdout.strip()
                CONSOLE.print(f"  [red]❌ Execution Failed:[/red]\n  [red]{error_msg[:300]}[/red]")
                
                if attempt == MAX_AUTO_REPAIR_ATTEMPTS: break
                
                CONSOLE.print("  [magenta]🤖 Feeding error back to AI to fix...[/magenta]")
                fix_prompt = f"The code failed with this error:\n\n{error_msg}\n\nFix the code. Output ONLY the raw fixed code."
                response = call_model(fix_prompt, stream=False)
                if response:
                    new_code = extract_code(response)
                    Path(tmp_path).write_text(new_code, encoding="utf-8")
                    code = new_code
        except subprocess.TimeoutExpired:
            CONSOLE.print("  [yellow]⚠ Script timed out. Assuming infinite loop. Stopping auto-fix.[/yellow]")
            break
            
    if Path(tmp_path).exists(): Path(tmp_path).unlink()
    return code

def run_unit_tests(code: str) -> None:
    if STATE.lang != "Python":
        CONSOLE.print("  [dim]Unit test generation currently optimized for Python (pytest).[/dim]")
        return
        
    CONSOLE.print("\n[bold cyan]● Step 5 — Generating & Running Unit Tests…[/bold cyan]")
    response = call_model(build_test_prompt(code), stream=False)
    if not response: return
    
    test_code = extract_code(response)
    test_path = "test_kodedoc.py"
    Path(test_path).write_text(test_code, encoding="utf-8")
    
    CONSOLE.print("  [*] Running pytest...")
    try:
        res = subprocess.run([sys.executable, "-m", "pytest", test_path, "-v"], capture_output=True, text=True, timeout=30)
        if res.returncode == 0:
            CONSOLE.print("  [green]✔ All unit tests passed![/green]")
            for line in res.stdout.splitlines():
                if "PASSED" in line: CONSOLE.print(f"  [dim]{line}[/dim]")
        else:
            CONSOLE.print("  [red]❌ Unit tests failed:[/red]")
            for line in res.stdout.splitlines():
                if "FAILED" in line or "Error" in line: CONSOLE.print(f"  [red]{line}[/red]")
    except Exception as e:
        CONSOLE.print(f"  [red]Test Runner Error: {e}[/red]")
    finally:
        if Path(test_path).exists(): Path(test_path).unlink()

def select_model_flow() -> None:
    try:
        resp = requests.get(f"{STATE.ollama_url}/api/tags", timeout=5).json()
        models = [m["name"] for m in resp.get("models", [])]
        if not models:
            CONSOLE.print("  [red]No models found.[/red]"); return
        for i, m in enumerate(models, 1): CONSOLE.print(f"  [{i}] {m}")
        idx = int(Prompt.ask("Select model", default="1")) - 1
        STATE.model = models[idx]
        CONSOLE.print(f"  [green]✔ Bound to {STATE.model}[/green]")
    except Exception as e:
        CONSOLE.print(f"  [red]Connection failed: {e}[/red]")

def select_language_flow() -> None:
    # Build a numbered grid table
    table = Table(show_header=False, box=box.SIMPLE, border_style="dim", padding=(0, 1))
    cols = 4
    for _ in range(cols * 2):
        table.add_column()
        
    for i in range(0, len(LANGUAGES), cols):
        row = LANGUAGES[i:i + cols]
        padded = row + [""] * (cols - len(row))
        styled_row = []
        
        for idx, lang in enumerate(padded):
            if not lang:
                styled_row.extend(["", ""])
                continue
                
            num = i + idx + 1
            if lang == STATE.lang:
                # Highlight current language
                styled_row.extend([f"[bold cyan]{num:>2}.[/bold cyan]", f"[bold green]{lang}[/bold green]"])
            else:
                styled_row.extend([f"[dim]{num:>2}.[/dim]", f"[white]{lang}[/white]"])
                
        table.add_row(*styled_row)

    CONSOLE.print("\n[bold cyan]Select a Language[/bold cyan]")
    CONSOLE.print(table)
    
    choice = Prompt.ask("  Enter number (or type name)", default=str(LANGUAGES.index(STATE.lang) + 1) if STATE.lang in LANGUAGES else "1", console=CONSOLE).strip()
    
    # Check if they typed a number
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(LANGUAGES):
            STATE.lang = LANGUAGES[idx]
            CONSOLE.print(f"  [green]✔ Language set → {STATE.lang}[/green]")
            return
        else:
            CONSOLE.print("  [red]Invalid number.[/red]")
            return
            
    # Check if they typed the name manually
    match = next((l for l in LANGUAGES if l.lower() == choice.lower()), None)
    if match:
        STATE.lang = match
        CONSOLE.print(f"  [green]✔ Language set → {STATE.lang}[/green]")
    else:
        CONSOLE.print(f"  [red]'{escape(choice)}' is not in the supported list.[/red]")

def load_file_flow() -> str:
    path = Prompt.ask("File path").strip()
    if not os.path.isfile(path): return ""
    try:
        content = open(path, "r", encoding="utf-8").read()
        ext = os.path.splitext(path)[1].lower()
        detected = EXT_TO_LANG.get(ext)
        if detected:
            STATE.lang = detected
            CONSOLE.print(f"  [dim]Auto-detected language: {STATE.lang}[/dim]")
        return content
    except Exception as e:
        CONSOLE.print(f"  [red]Read error: {e}[/red]"); return ""

def copy_output() -> None:
    if not STATE.last_fixed:
        CONSOLE.print("  [red]Nothing to copy. Run 'debug' or 'convert' first.[/red]")
        return
    
    CONSOLE.print("  [dim]Copying to clipboard...[/dim]")
    
    # Try clip.exe (WSL) but with a timeout so it doesn't hang
    try:
        process = subprocess.Popen(['clip.exe'], stdin=subprocess.PIPE, text=True)
        process.communicate(input=STATE.last_fixed, timeout=3)
        CONSOLE.print("  [green]✔ Copied to clipboard![/green]")
        return
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        # If clip.exe hangs, kill it and try the fallback
        process.kill()
    
    # Linux/Mac fallbacks
    text = STATE.last_fixed.encode()
    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            proc.communicate(text, timeout=3)
            CONSOLE.print("  [green]✔ Copied to clipboard[/green]")
            return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
            
    # Python pyperclip fallback
    try:
        import pyperclip
        pyperclip.copy(STATE.last_fixed)
        CONSOLE.print("  [green]✔ Copied to clipboard[/green]")
    except Exception:
        CONSOLE.print("  [red]✖ Clipboard failed. Use 'export' to save to a file instead.[/red]")
def run_debug(code: str) -> None:
    if not STATE.lang or not STATE.model or not code.strip(): return
    STATE.running = True
    
    CONSOLE.print(f"\n[bold cyan]● Step 1 — Deep analysis ({STATE.lang})…[/bold cyan]")
    analysis = call_model(build_analysis_prompt(code), stream=True)
    if not analysis: STATE.running = False; return
    CONSOLE.print(f"  [yellow]{len(analysis.splitlines())} lines of analysis generated.[/yellow]")
    
    CONSOLE.print("\n[bold cyan]● Step 2 — Auto-Installing Dependencies…[/bold cyan]")
    deps = extract_dependencies(analysis, code)
    if deps: run_install(deps)
    else: CONSOLE.print("  [green]✔ No missing dependencies detected.[/green]")
    
    CONSOLE.print("\n[bold cyan]● Step 3 — Rewriting and Fixing…[/bold cyan]")
    fix_response = call_model(build_fix_prompt(code, analysis), stream=False)
    if not fix_response: 
        STATE.running = False
        return
    fixed = extract_code(fix_response)
    
    if not fixed:
        CONSOLE.print("  [red]✖ The model failed to output the full code (it used placeholders).[/red]")
        CONSOLE.print("  [yellow]Try switching to a smarter model like 'qwen2.5-coder'.[/yellow]")
        STATE.running = False
        return
    
    CONSOLE.print("\n[bold cyan]● Step 4 — Agentic Verification Loop…[/bold cyan]")
    fixed = execute_and_fix(fixed)
    
    run_unit_tests(fixed)
    
    STATE.last_fixed = fixed
    CONSOLE.print("\n[bold green]✔ Debug complete.[/bold green]")
    try:
        CONSOLE.print(Panel(Syntax(fixed, (STATE.lang or "text").lower(), theme="monokai", line_numbers=True), title="[bold green]Final Code[/bold green]"))
    except:
        CONSOLE.print(Panel(fixed, title="[bold green]Final Code[/bold green]"))
    
    if STATE.diff_mode:
        CONSOLE.rule("[cyan]Diff[/cyan]")
        diff = difflib.unified_diff(code.splitlines(), fixed.splitlines(), lineterm="")
        for line in diff:
            if line.startswith("+"): CONSOLE.print(f"[green]{escape(line)}[/green]")
            elif line.startswith("-"): CONSOLE.print(f"[red]{escape(line)}[/red]")
            elif not line.startswith("---") and not line.startswith("+++"): CONSOLE.print(f"[dim]{escape(line)}[/dim]")
            
    STATE.running = False

HELP_TABLE = [
    ("model", "Pick an Ollama model"),
    ("lang", "Select a programming language"),
    ("load", "Load a local file"),
    ("debug", "Full pipeline (Analysis -> Deps -> Fix -> Execute -> Test)"),
    ("convert", "Translate current buffer to another language"),
    ("chat", "Enter interactive AI chat mode"),
    ("copy", "Copy fixed code to clipboard"),
    ("diff", "Toggle unified diff view"),
    ("clear", "Reset the code buffer"),
    ("quit", "Exit KodeDoc"),
]

def main() -> None:
    CONSOLE.print(Panel(f"[bold cyan]KodeDoc v{VERSION} — Autonomous Edition[/bold cyan]\n[dim]Type 'help' for commands.[/dim]"))
    code_buffer = ""

    while True:
        try:
            raw = Prompt.ask(f"\n[{STATE.lang or 'no-lang'}] >").strip()
        except (KeyboardInterrupt, EOFError): break
        if not raw: continue
        
        parts = raw.split(None, 1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd in ("quit", "exit", "q"): break
        elif cmd == "help":
            for c, d in HELP_TABLE: CONSOLE.print(f"  [cyan]{c:<10}[/cyan] {d}")
        elif cmd == "model": select_model_flow()
        elif cmd == "lang": select_language_flow()
        elif cmd == "load": 
            text = load_file_flow()
            if text: code_buffer = text
        elif cmd == "clear": code_buffer = ""
        elif cmd == "debug":
            if not code_buffer.strip():
                CONSOLE.print("  [red]✖ Buffer is empty. Use 'load' to read a file first.[/red]")
            elif not STATE.model:
                CONSOLE.print("  [red]✖ No model selected. Use 'model' first.[/red]")
            elif not STATE.lang:
                CONSOLE.print("  [red]✖ No language selected. Use 'lang' first.[/red]")
            else:
                run_debug(code_buffer)
        elif cmd == "diff": STATE.diff_mode = not STATE.diff_mode
        elif cmd == "copy": copy_output()
        elif cmd == "convert":
            if not code_buffer or not STATE.lang: continue
            target = Prompt.ask("Target language")
            if target:
                CONSOLE.print(f"  [*] Converting to {target}...")
                res = call_model(build_convert_prompt(code_buffer, target), stream=True)
                if res:
                    code_buffer = extract_code(res)
                    STATE.lang = target
                    CONSOLE.print(f"  [green]✔ Converted to {target}.[/green]")
        elif cmd == "chat":
            if not STATE.model: 
                CONSOLE.print("  [red]Select a model first.[/red]")
                continue
            CONSOLE.print("\n  [bold cyan]Chat Mode (type 'exit' to return)[/bold cyan]")
            while True:
                msg = Prompt.ask("  [magenta]You[/magenta]")
                if msg.lower() in ("exit", "quit", "back"): break
                if not msg.strip(): continue
                
                STATE.chat_history.append({"role": "user", "content": msg})
                try:
                    res = requests.post(
                        f"{STATE.ollama_url}/api/chat", 
                        json={"model": STATE.model, "messages": STATE.chat_history[-10:], "stream": False},
                        timeout=120
                    )
                    res.raise_for_status()
                    reply = res.json().get("message", {}).get("content", "")
                    CONSOLE.print(Panel(reply, border_style="cyan", title="KodeDoc"))
                    STATE.chat_history.append({"role": "assistant", "content": reply})
                except Exception as e:
                    CONSOLE.print(f"  [red]Chat error: {e}[/red]")
        else:
            CONSOLE.print(f"  [red]Unknown command: {cmd}[/red]")

if __name__ == "__main__":
    main()
    
