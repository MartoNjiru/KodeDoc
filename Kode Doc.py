"""
KodeDoc — AI Debug Agent  v2.2.0
=================================
Connects to a remote/cloud Ollama instance for AI-powered code debugging,
with Anthropic Claude API as fallback.

What's new in v2.2.0:
  • FIXED: PHP $variable pattern no longer fires on every Python assignment;
    patterns are now language-aware and require stronger anchors.
  • FIXED: Inconsistent extraction threshold (0.5 vs 0.6) unified to 0.6.
  • FIXED: Claude model id corrected to a stable public identifier.
  • FIXED: simulate_install replaced with optional real install (still
    opt-in via 'auto always') so dependency flow is honest.
  • FIXED: Polyglot signatures tightened to reduce false positives inside
    strings/comments where practical.
  • FIXED: Session log now stores only metadata + truncated samples
    (avoids unbounded memory growth on large files).
  • NEW:   'status' command for a full status dump.
  • NEW:   'url' command to set Ollama base URL without restarting.
  • NEW:   Timeout and connection errors surface cleaner messages.
  • NEW:   Post-debug summary includes polyglot + verify verdicts
    in a compact table.
  • NEW:   Soft length guard: warns when code > ~80k chars before
    sending to the model (still allows the attempt).
  • v2.1 features retained: polyglot repair loop, 5× verify,
    1 GB local load ceiling, unified diff, streaming, history.

Dependencies (install before running):
    pip install requests rich

Optional:
    pip install anthropic   # only if you rely on Claude fallback via the
                            # official SDK; the HTTP fallback works without it.

Usage:
    python kodedoc.py

Optional model budgets (set before launch; the selected model/server must
support the requested values; a 1 GB local file limit does not mean a model can
process a 1 GB prompt in one request):
    $env:KODEDOC_CONTEXT_TOKENS = "65536"
    $env:KODEDOC_GENERATION_TOKENS = "32768"
    $env:KODEDOC_CLAUDE_MAX_TOKENS = "8192"
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import difflib
import tempfile
import datetime
import subprocess
from typing import Optional, Any

try:
    import requests
except ImportError:
    print("[ERROR] 'requests' not installed. Run: pip install requests")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
    from rich.rule import Rule
    from rich import box
    from rich.progress import (
        Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn,
    )
    from rich.markup import escape
except ImportError:
    print("[ERROR] 'rich' not installed. Run: pip install rich")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

CONSOLE = Console()

VERSION = "2.2.0"

MAX_LOAD_BYTES = 1 * 1024 * 1024 * 1024          # 1 GB local file ceiling


def _positive_int_setting(name: str, default: int) -> int:
    """Read a positive integer token budget from the environment."""
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
        if value > 0:
            return value
    except ValueError:
        pass
    print(f"[WARNING] {name} must be a positive integer; using {default}.")
    return default


MAX_GENERATION_TOKENS = _positive_int_setting(
    "KODEDOC_GENERATION_TOKENS", 32768
)
MODEL_CONTEXT_TOKENS = _positive_int_setting(
    "KODEDOC_CONTEXT_TOKENS", 65536
)
CLAUDE_MAX_TOKENS = _positive_int_setting(
    "KODEDOC_CLAUDE_MAX_TOKENS", 8192
)
VERIFY_PASSES = 5
MAX_POLY_REPAIR_ATTEMPTS = 3
SOFT_CHAR_WARN = 80_000                           # warn before sending large prompts

LANGUAGES = [
    "Python", "JavaScript", "TypeScript", "C", "C++", "C#", "Java",
    "Go", "Rust", "PHP", "Ruby", "Swift", "Kotlin", "Dart", "R",
    "Scala", "Lua", "Perl", "Shell", "PowerShell", "SQL", "HTML",
    "CSS", "YAML", "JSON", "XML", "Markdown", "Assembly", "MATLAB", "Julia",
]

DEP_MANAGERS = {
    "Python":     {"cmd": "pip install",                    "ext": ".py"},
    "JavaScript": {"cmd": "npm install",                    "ext": ".js"},
    "TypeScript": {"cmd": "npm install",                    "ext": ".ts"},
    "Ruby":       {"cmd": "gem install",                    "ext": ".rb"},
    "PHP":        {"cmd": "composer require",               "ext": ".php"},
    "Dart":       {"cmd": "dart pub add",                   "ext": ".dart"},
    "Java":       {"cmd": "mvn dependency:get -Dartifact=", "ext": ".java"},
    "Go":         {"cmd": "go get",                         "ext": ".go"},
    "Rust":       {"cmd": "cargo add",                      "ext": ".rs"},
    "Swift":      {"cmd": "swift package add",              "ext": ".swift"},
    "Kotlin":     {"cmd": "gradle implementation",          "ext": ".kt"},
    "R":          {"cmd": "install.packages",               "ext": ".R"},
    "C":          {"cmd": "apt install / vcpkg install",    "ext": ".c"},
    "C++":        {"cmd": "apt install / vcpkg install",    "ext": ".cpp"},
    "C#":         {"cmd": "dotnet add package",             "ext": ".cs"},
    "Shell":      {"cmd": "apt install / brew install",     "ext": ".sh"},
    "PowerShell": {"cmd": "Install-Module",                 "ext": ".ps1"},
    "Lua":        {"cmd": "luarocks install",               "ext": ".lua"},
    "Perl":       {"cmd": "cpanm",                          "ext": ".pl"},
    "Scala":      {"cmd": "sbt libraryDependencies +=",     "ext": ".scala"},
}

EXT_TO_LANG = {v["ext"]: k for k, v in DEP_MANAGERS.items()}
EXT_TO_LANG.update({
    ".html": "HTML", ".htm": "HTML",
    ".css":  "CSS",
    ".yaml": "YAML", ".yml": "YAML",
    ".json": "JSON",
    ".xml":  "XML",
    ".md":   "Markdown",
    ".s":    "Assembly", ".asm": "Assembly",
    ".m":    "MATLAB",
    ".jl":   "Julia",
    ".r":    "R", ".R": "R",
})

PYTHON_STDLIB = {
    "os", "sys", "re", "io", "math", "time", "json", "csv", "copy", "abc", "ast",
    "cgi", "cmd", "code", "dis", "gc", "hmac", "imaplib", "imp", "inspect",
    "logging", "pathlib", "pickle", "platform", "pprint", "queue", "random",
    "shutil", "signal", "socket", "sqlite3", "stat", "struct", "subprocess",
    "tempfile", "threading", "traceback", "typing", "unittest", "urllib",
    "uuid", "warnings", "weakref", "xml", "zipfile", "zlib", "hashlib",
    "datetime", "collections", "functools", "itertools", "contextlib", "enum",
    "string", "textwrap", "fnmatch", "glob", "linecache", "codecs",
    "unicodedata", "html", "http", "email", "smtplib", "ftplib", "numbers",
    "decimal", "fractions", "statistics", "cmath", "array", "heapq", "bisect",
    "operator", "dataclasses", "pdb", "timeit", "doctest", "difflib",
    "builtins", "_thread", "concurrent", "asyncio", "multiprocessing",
    "importlib", "pkgutil", "zipimport", "sysconfig", "argparse",
    "configparser", "shlex", "readline", "rlcompleter", "select", "selectors",
    "pty", "termios", "tty", "fcntl", "resource", "syslog", "mmap", "ctypes",
    "ssl", "xmlrpc",
}

# ─────────────────────────────────────────────────────────────────────────────
#  POLYGLOT CONFLICT MAP  (v2.2 — tightened anchors, fewer false positives)
# ─────────────────────────────────────────────────────────────────────────────

POLYGLOT_SIGNATURES: dict[str, list[tuple[str, str]]] = {
    "Python": [
        # JavaScript / TypeScript — strong anchors
        ("JavaScript", r"\bconsole\.(log|warn|error|info|debug)\s*\("),
        ("JavaScript", r"\bdocument\.(getElementById|querySelector|createElement)\b"),
        ("JavaScript", r"\bwindow\.(location|alert|confirm|open)\b"),
        ("JavaScript", r"\brequire\s*\(\s*['\"][^'\"]+['\"]\s*\)"),
        ("JavaScript", r"^\s*import\s+\{[^}]+\}\s+from\s+['\"]"),
        ("JavaScript", r"\bPromise\.(resolve|reject|all|race)\b"),
        ("JavaScript", r"\basync\s+function\b"),
        ("JavaScript", r"=>\s*[\{\(]"),
        # avoid bare var/let/const — too many false hits on comments
        ("JavaScript", r"^\s*(var|let|const)\s+[a-zA-Z_$][\w$]*\s*="),
        # Java
        ("Java", r"\bSystem\.out\.print(ln|f)?\s*\("),
        ("Java", r"\bpublic\s+static\s+void\s+main\s*\("),
        ("Java", r"\bpublic\s+(class|interface|enum)\s+\w"),
        ("Java", r"import\s+java\.\w"),
        ("Java", r"\bArrayList\s*<"),
        ("Java", r"\bHashMap\s*<"),
        # Ruby
        ("Ruby", r"^\s*puts\s+"),
        ("Ruby", r"\battr_(accessor|reader|writer)\b"),
        ("Ruby", r"\.each\s+do\s+\|"),
        # PHP — require <?php or explicit echo; avoid bare $var=
        ("PHP", r"<\?php"),
        ("PHP", r"^\s*echo\s+['\"\$]"),
        # C / C++
        ("C", r"#include\s*<[a-z_]+\.h>"),
        ("C", r"\bprintf\s*\("),
        ("C", r"\bmalloc\s*\("),
        ("C++", r"\bstd::\w"),
        ("C++", r"\bcout\s*<<"),
        ("C++", r"\bcin\s*>>"),
        # Shell
        ("Shell", r"^#!/bin/(bash|sh|zsh)"),
        ("Shell", r"^\s*echo\s+['\"]"),
        # PowerShell
        ("PowerShell", r"Write-(Host|Output)\s"),
    ],
    "JavaScript": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("Python", r"^\s*class\s+\w+\s*(\(|:)"),
        ("Python", r"^\s*from\s+\w+\s+import\b"),
        ("Python", r"^\s*print\s*\("),
        ("Python", r"^\s*elif\s+"),
        ("Python", r"^\s*except\s+\w+\s*:"),
        ("Java", r"\bSystem\.out\.print"),
        ("Java", r"\bpublic\s+static\s+void\s+main\b"),
        ("Java", r"import\s+java\."),
        ("Ruby", r"^\s*puts\s+"),
        ("PHP", r"<\?php"),
        ("PHP", r"^\s*echo\s+"),
    ],
    "TypeScript": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("Python", r"^\s*from\s+\w+\s+import\b"),
        ("Python", r"^\s*print\s*\("),
        ("Java", r"\bSystem\.out\.print"),
        ("Java", r"import\s+java\."),
        ("Ruby", r"^\s*puts\s+"),
        ("PHP", r"<\?php"),
    ],
    "Java": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("Python", r"^\s*print\s*\("),
        ("Python", r"^\s*from\s+\w+\s+import\b"),
        ("JavaScript", r"\bconsole\.(log|warn|error)\s*\("),
        ("JavaScript", r"^\s*(var|let|const)\s+\w+\s*="),
        ("JavaScript", r"=>\s*\{"),
        ("Ruby", r"^\s*puts\s+"),
        ("PHP", r"<\?php"),
        ("PHP", r"^\s*echo\s+"),
        ("C++", r"\bcout\s*<<"),
        ("C++", r"\bstd::\w"),
    ],
    "Go": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("Python", r"^\s*print\s*\("),
        ("JavaScript", r"\bconsole\.(log|warn|error)\s*\("),
        ("JavaScript", r"=>\s*\{"),
        ("Java", r"\bSystem\.out\.print"),
        ("Java", r"import\s+java\."),
        ("Ruby", r"^\s*puts\s+"),
        ("PHP", r"<\?php"),
        ("C++", r"\bcout\s*<<"),
        ("C++", r"\bstd::\w"),
    ],
    "Rust": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("Python", r"^\s*print\s*\("),
        ("JavaScript", r"\bconsole\.(log|warn|error)\s*\("),
        ("Java", r"\bSystem\.out\.print"),
        ("Java", r"import\s+java\."),
        ("Ruby", r"^\s*puts\s+"),
        ("PHP", r"<\?php"),
        ("C++", r"\bcout\s*<<"),
    ],
    "C": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("Python", r"^\s*print\s*\("),
        ("JavaScript", r"\bconsole\.(log|warn|error)\s*\("),
        ("Java", r"\bSystem\.out\.print"),
        ("Java", r"import\s+java\."),
        ("Ruby", r"^\s*puts\s+"),
        ("PHP", r"<\?php"),
        ("C++", r"\bcout\s*<<"),
        ("C++", r"\bstd::\w"),
    ],
    "C++": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("Python", r"^\s*print\s*\("),
        ("JavaScript", r"\bconsole\.(log|warn|error)\s*\("),
        ("Java", r"\bSystem\.out\.print"),
        ("Java", r"import\s+java\."),
        ("Ruby", r"^\s*puts\s+"),
        ("PHP", r"<\?php"),
    ],
    "C#": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("Python", r"^\s*print\s*\("),
        ("JavaScript", r"\bconsole\.(log|warn|error)\s*\("),
        ("Java", r"\bSystem\.out\.print"),
        ("Java", r"import\s+java\."),
        ("Ruby", r"^\s*puts\s+"),
        ("PHP", r"<\?php"),
        ("C++", r"\bcout\s*<<"),
    ],
    "Ruby": [
        ("Python", r"^\s*def\s+\w+\([^)]*\)\s*:"),
        ("Python", r"^\s*print\s*\("),
        ("JavaScript", r"\bconsole\.(log|warn|error)\s*\("),
        ("JavaScript", r"^\s*(const|let)\s+\w+\s*="),
        ("PHP", r"<\?php"),
        ("Java", r"\bSystem\.out\.print"),
        ("C++", r"\bcout\s*<<"),
    ],
    "PHP": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("Python", r"^\s*print\s*\("),
        ("JavaScript", r"(?<!\$)\bconsole\.(log|warn|error)\s*\("),
        ("Ruby", r"^\s*puts\s+"),
        ("Java", r"\bSystem\.out\.print"),
        ("C++", r"\bcout\s*<<"),
    ],
    "Swift": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("Python", r"^\s*print\s*\("),
        ("JavaScript", r"\bconsole\.(log|warn|error)\s*\("),
        ("Java", r"\bSystem\.out\.print"),
        ("Ruby", r"^\s*puts\s+"),
        ("PHP", r"<\?php"),
    ],
    "Kotlin": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("Python", r"^\s*print\s*\("),
        ("JavaScript", r"\bconsole\.(log|warn|error)\s*\("),
        ("Java", r"import\s+java\."),
        ("Ruby", r"^\s*puts\s+"),
        ("PHP", r"<\?php"),
    ],
    "HTML": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("Python", r"^\s*import\s+\w+"),
        ("PHP", r"<\?php"),
        ("Ruby", r"^\s*puts\s+"),
    ],
    "SQL": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("JavaScript", r"\bconsole\.(log|warn|error)\s*\("),
        ("Java", r"\bSystem\.out\.print"),
        ("PHP", r"<\?php"),
    ],
    "Shell": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("JavaScript", r"\bconsole\.(log|warn|error)\s*\("),
        ("PHP", r"<\?php"),
        ("Java", r"\bSystem\.out\.print"),
    ],
    "Lua": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("JavaScript", r"\bconsole\.(log|warn|error)\s*\("),
        ("PHP", r"<\?php"),
        ("Java", r"\bSystem\.out\.print"),
    ],
    "Perl": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("JavaScript", r"\bconsole\.(log|warn|error)\s*\("),
        ("Java", r"\bSystem\.out\.print"),
    ],
    "R": [
        ("Python", r"^\s*def\s+\w+\s*\("),
        ("JavaScript", r"\bconsole\.(log|warn|error)\s*\("),
        ("Java", r"\bSystem\.out\.print"),
    ],
    "Scala": [
        ("Python", r"^\s*def\s+\w+\s*\(.*\)\s*:"),
        ("JavaScript", r"\bconsole\.(log|warn|error)\s*\("),
        ("PHP", r"<\?php"),
    ],
}

# ─────────────────────────────────────────────────────────────────────────────
#  CLOUD MODEL CATALOGUE (reference only — picker uses live Ollama list)
# ─────────────────────────────────────────────────────────────────────────────

CLOUD_MODELS = [
    {"name": "codellama:7b",        "desc": "Meta · Code specialist · Best for debugging",    "tag": "RECOMMENDED"},
    {"name": "codellama:13b",       "desc": "Meta · Larger, higher accuracy",                 "tag": ""},
    {"name": "codellama:34b",       "desc": "Meta · Best quality, slower",                    "tag": ""},
    {"name": "deepseek-coder:6.7b", "desc": "DeepSeek · Fast, code-focused",                 "tag": ""},
    {"name": "deepseek-coder:33b",  "desc": "DeepSeek · Large code model, very accurate",     "tag": ""},
    {"name": "qwen2.5-coder:7b",    "desc": "Alibaba · Code specialist, multi-language",      "tag": ""},
    {"name": "qwen2.5-coder:14b",   "desc": "Alibaba · Larger, stronger reasoning",           "tag": ""},
    {"name": "starcoder2:7b",       "desc": "Bigcode · 80+ languages, fast",                  "tag": ""},
    {"name": "starcoder2:15b",      "desc": "Bigcode · Larger, higher precision",             "tag": ""},
    {"name": "wizard-coder:7b",     "desc": "WizardLM · Instruction-tuned code generation",  "tag": ""},
    {"name": "phind-codellama:34b", "desc": "Phind · Code + natural language explanation",   "tag": ""},
    {"name": "mistral:7b",          "desc": "Mistral AI · General + code, very fast",         "tag": ""},
    {"name": "llama3.1:8b",         "desc": "Meta · General purpose, strong reasoning",       "tag": ""},
    {"name": "llama3.1:70b",        "desc": "Meta · Large, best general accuracy",            "tag": ""},
    {"name": "gemma2:9b",           "desc": "Google · Efficient, good code understanding",    "tag": ""},
]

SAMPLES = {
    "Python": """\
import os
import jsn  # wrong module name

def calculat_sum(numers):
    totl = 0
    for n in numers
        totl =+ n  # += not =+
    return totl

reslt = calculat_sum([1, 2, 3, 4, 5])
prnt(f"Sum: {reslt}")  # wrong function name

if __name__ == "__main":  # missing closing quote
    main()
""",
    "JavaScript": """\
const fech = require('node-fech')  // wrong package

async funtion getData(url) {
  const res = awit fech(url)
  const data = res.jso()  // missing ()
  consle.log(data)  // typo
}

getData('https://api.example.com/data
         .then(() => console.log('done'))
""",
    "Java": """\
import java.util.ArryList;  // typo

public class Main {
    public static void mian(String[] args) {  // typo
        ArryList<String> list = new ArryList<>();
        list.ad("hello");  // wrong method
        list.ad("world");

        for (String s : list) {
            System.out.printl(s);  // typo
        }
    }
}
""",
    "Go": """\
package main

imprt "fmt"  // typo

func add(a int b int) int {  // missing comma
    retrun a + b  // typo
}

func main() {
    reslt := add(3, 4)
    fmt.Printl("Result:", reslt)  // typo
}
""",
    "C": """\
#include <stdio.h
#include <stdlib.h>

int main() {
    int arr[] = {5, 3, 1, 4, 2};
    int n = sizof(arr) / sizof(arr[0]);  // typos

    for (int i = 0; i < n; i++) {
        for (int j = 0; j < n-i-1; j++) {
            if (arr[j] > arr[j+1]) {
                int tmp = arr[j]
                arr[j] = arr[j+1];
                arr[j+1] = tmp;
            }
        }
    }

    for (int i = 0; i < n; i++)
        printf("%d ", arr[i]
    return 0;
}
""",
    "Python_polyglot": """\
import os

def process_data(items):
    results = []
    for item in items:
        console.log("Processing:", item)   # JS — should be print()
        var result = item * 2              # JS — should be: result = item * 2
        results.append(result)
    return results

data = [1, 2, 3, 4, 5]
output = process_data(data)
System.out.println(output)                # Java — should be: print(output)
""",
}

# ─────────────────────────────────────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────────────────────────────────────

class State:
    def __init__(self) -> None:
        self.model: Optional[str] = None
        self.ollama_url: str = "http://localhost:11434"
        self.lang: Optional[str] = None
        self.last_fixed: str = ""
        self.diff_mode: bool = False
        self.auto_install: str = "ask"   # "ask" | "always" | "never"
        self.session_log: list[dict[str, Any]] = []
        self.running: bool = False

STATE = State()

# ─────────────────────────────────────────────────────────────────────────────
#  OLLAMA / CLAUDE API
# ─────────────────────────────────────────────────────────────────────────────

def call_ollama(prompt: str, stream: bool = False) -> Optional[str]:
    """Call Ollama generate endpoint. Increased timeout for large workloads."""
    if not STATE.model:
        CONSOLE.print("  [red]✖ No model selected.[/red]")
        return None
    url = f"{STATE.ollama_url.rstrip('/')}/api/generate"
    payload = {
        "model": STATE.model,
        "prompt": prompt,
        "stream": stream,
        "options": {
            "temperature": 0.05,
            "num_predict": MAX_GENERATION_TOKENS,
            "top_p": 0.9,
            "repeat_penalty": 1.1,
            "num_ctx": MODEL_CONTEXT_TOKENS,
        },
    }
    try:
        if stream:
            return _stream_ollama(url, payload)
        resp = requests.post(url, json=payload, timeout=300)
        resp.raise_for_status()
        data = resp.json()
        if data.get("done_reason") == "length":
            CONSOLE.print(
                "  [yellow]⚠ Ollama stopped at the generation limit; "
                "the response is incomplete.[/yellow]"
            )
            return None
        return data.get("response", "") or None
    except requests.exceptions.ConnectionError:
        CONSOLE.print(f"  [red]✖ Cannot connect to {STATE.ollama_url}[/red]")
        CONSOLE.print("  [dim]Verify your Ollama endpoint is reachable.[/dim]")
        return None
    except requests.exceptions.Timeout:
        CONSOLE.print("  [red]✖ Ollama request timed out.[/red]")
        return None
    except Exception as exc:
        CONSOLE.print(f"  [red]✖ Ollama error: {escape(str(exc))}[/red]")
        return None


def _stream_ollama(url: str, payload: dict) -> Optional[str]:
    """Stream response, print live, return full text."""
    full = ""
    done_reason = None
    generation_finished = False
    p = dict(payload)
    p["stream"] = True
    try:
        with requests.post(url, json=p, stream=True, timeout=360) as resp:
            resp.raise_for_status()
            CONSOLE.print("\n  [bold cyan]── Fixed Code (streaming) ──[/bold cyan]")
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                    chunk = obj.get("response", "")
                    full += chunk
                    print(chunk, end="", flush=True)
                    if obj.get("done"):
                        generation_finished = True
                        done_reason = obj.get("done_reason")
                except json.JSONDecodeError:
                    pass
        print()
        if done_reason == "length":
            CONSOLE.print(
                "  [yellow]⚠ Ollama stopped at the generation limit; "
                "the response is incomplete.[/yellow]"
            )
            return None
        if not generation_finished:
            CONSOLE.print(
                "  [yellow]⚠ Ollama stream ended before a completion signal; "
                "discarding the partial response.[/yellow]"
            )
            return None
        return full or None
    except requests.exceptions.ConnectionError:
        CONSOLE.print("  [red]✖ Stream lost — connection dropped.[/red]")
        return None
    except Exception as exc:
        CONSOLE.print(f"  [red]✖ Stream error: {escape(str(exc))}[/red]")
        return None


def call_claude_api(prompt: str) -> Optional[str]:
    """Fallback: Anthropic Claude API via HTTP."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        CONSOLE.print("  [dim]ANTHROPIC_API_KEY not set — Claude fallback unavailable.[/dim]")
        return None
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": api_key,
    }
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": CLAUDE_MAX_TOKENS,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=300,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("stop_reason") == "max_tokens":
            CONSOLE.print(
                "  [yellow]⚠ Claude stopped at the generation limit; "
                "the response is incomplete.[/yellow]"
            )
            return None
        return "".join(b.get("text", "") for b in data.get("content", [])) or None
    except Exception as exc:
        CONSOLE.print(f"  [red]✖ Claude API error: {escape(str(exc))}[/red]")
        return None


def call_model(prompt: str, stream: bool = False) -> Optional[str]:
    """Try Ollama, fall back to Claude."""
    result = call_ollama(prompt, stream=stream)
    if result is not None:
        return result
    CONSOLE.print("  [yellow]Ollama unreachable — falling back to Claude API…[/yellow]")
    result = call_claude_api(prompt)
    if result is None:
        CONSOLE.print("  [red]✖ Both Ollama and Claude API failed.[/red]")
        CONSOLE.print("  [dim]Check Ollama URL or set ANTHROPIC_API_KEY.[/dim]")
    return result

# ─────────────────────────────────────────────────────────────────────────────
#  POLYGLOT CONFLICT DETECTION  (static — no AI call)
# ─────────────────────────────────────────────────────────────────────────────

def detect_polyglot_conflicts(code: str) -> list[dict[str, Any]]:
    """
    Scan source lines for patterns belonging to a secondary language while
    STATE.lang is the primary language.

    Returns list of dicts: {line_no, secondary_lang, pattern, line_text}.
    line_text is raw — no Rich escaping. Display callers must escape().
    """
    if not STATE.lang:
        return []
    sigs = POLYGLOT_SIGNATURES.get(STATE.lang, [])
    if not sigs:
        return []

    conflicts: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()

    for i, line in enumerate(code.splitlines(), start=1):
        # skip obvious comments for a few languages to cut noise
        stripped = line.lstrip()
        if STATE.lang == "Python" and stripped.startswith("#"):
            continue
        if STATE.lang in ("JavaScript", "TypeScript", "Java", "C", "C++", "Go", "Rust", "C#") and (
            stripped.startswith("//") or stripped.startswith("/*")
        ):
            continue

        for secondary, pattern in sigs:
            if re.search(pattern, line):
                key = (i, secondary)
                if key not in seen:
                    seen.add(key)
                    conflicts.append({
                        "line_no": i,
                        "secondary_lang": secondary,
                        "pattern": pattern,
                        "line_text": line.rstrip(),
                    })
    return conflicts

# ─────────────────────────────────────────────────────────────────────────────
#  PROMPTS — escape() is NEVER used inside these builders
# ─────────────────────────────────────────────────────────────────────────────

def _numbered(code: str) -> str:
    return "\n".join(f"{i+1:5d} | {line}" for i, line in enumerate(code.splitlines()))


def build_analysis_prompt(code: str) -> str:
    n = _numbered(code)
    line_count = len(code.splitlines())
    return (
        f"You are a world-class {STATE.lang} engineer, code auditor, and static analysis expert.\n\n"
        f"TASK: Perform an exhaustive, line-by-line static analysis of the {STATE.lang} code below.\n"
        f"Find EVERY defect without exception. Missing one bug is a failure.\n\n"
        f"CHECKLIST — you MUST cover every category:\n"
        f" 1. Syntax errors: missing brackets, colons, semicolons, quotes, parentheses, braces\n"
        f" 2. Wrong or misspelled built-in function / method names\n"
        f" 3. Wrong or misspelled variable, class, or parameter names\n"
        f" 4. Missing, wrong, or fictitious import / require / include / using statements\n"
        f" 5. Logic errors: wrong operator, reversed condition, off-by-one, wrong sign\n"
        f" 6. Type errors: wrong type passed or returned\n"
        f" 7. Undefined variables or out-of-scope references\n"
        f" 8. Missing or unreachable return statements\n"
        f" 9. Incorrect API calls: wrong argument count or order\n"
        f"10. Language-specific illegal constructs and anti-patterns\n"
        f"11. Missing or incorrect package / dependency declarations\n"
        f"12. POLYGLOT CONTAMINATION — this is critical:\n"
        f"    Scan every line for syntax that belongs to a different language.\n"
        f"    Examples: JavaScript console.log() inside {STATE.lang}, Java System.out.println()\n"
        f"    inside {STATE.lang}, PHP $variable syntax inside {STATE.lang}, Ruby puts inside\n"
        f"    {STATE.lang}, C++ cout inside {STATE.lang}.\n"
        f"    For EVERY polyglot line: state the line number, identify the foreign language,\n"
        f"    quote the offending construct, and specify the exact {STATE.lang} equivalent.\n\n"
        f"OUTPUT FORMAT — one numbered line per issue:\n"
        f"1. Line 12: `jsn` should be `json` — wrong module name.\n"
        f"2. Line 15: missing colon after `for n in numers`.\n"
        f"3. Line 7: `console.log(x)` is JavaScript syntax — replace with `print(x)` in {STATE.lang}.\n\n"
        f"Code ({line_count} lines):\n"
        f"{n}\n\n"
        f"List ALL issues now. Be exhaustive and precise."
    )


def build_polyglot_fix_prompt(code: str, conflicts: list) -> str:
    n = _numbered(code)
    conflict_lines = []
    for c in conflicts:
        raw_line = c["line_text"]
        conflict_lines.append(
            f"  Line {c['line_no']}: [{c['secondary_lang']} syntax] {raw_line}"
        )
    conflict_desc = "\n".join(conflict_lines)

    return (
        f"You are a world-class {STATE.lang} engineer specialising in polyglot conflict resolution.\n\n"
        f"TASK: The code below is written in {STATE.lang} but contains lines that use syntax\n"
        f"from other programming languages. These are NOT valid {STATE.lang} — they must be\n"
        f"translated into correct, idiomatic {STATE.lang} that achieves the same intent.\n\n"
        f"DETECTED FOREIGN-LANGUAGE CONSTRUCTS:\n"
        f"{conflict_desc}\n\n"
        f"TRANSLATION REFERENCE (use these exact equivalents unless the context demands otherwise):\n"
        f"  JavaScript console.log(x)      → {STATE.lang} equivalent of print(x)\n"
        f"  JavaScript var/let/const x = v → {STATE.lang} assignment: x = v\n"
        f"  JavaScript x => {{ ... }}       → {STATE.lang} anonymous function syntax\n"
        f"  Java System.out.println(x)     → {STATE.lang} equivalent of print(x)\n"
        f"  Ruby puts x                    → {STATE.lang} equivalent of print(x)\n"
        f"  PHP $variable                  → {STATE.lang} variable (no $ prefix)\n"
        f"  PHP echo x                     → {STATE.lang} equivalent of print(x)\n"
        f"  C #include <header>            → {STATE.lang} import statement\n"
        f"  C++ cout << x                  → {STATE.lang} equivalent of print(x)\n\n"
        f"STRICT RULES:\n"
        f"  1. Translate EVERY line listed above into correct {STATE.lang} syntax.\n"
        f"  2. Preserve the original logic and intent of each translated line exactly.\n"
        f"  3. Do NOT alter any line that is already valid {STATE.lang}.\n"
        f"  4. Do NOT introduce any new foreign-language constructs.\n"
        f"  5. Output ONLY the complete rewritten {STATE.lang} code.\n"
        f"  6. Do NOT include markdown fences, backticks, commentary, or explanations.\n"
        f"  7. Do NOT truncate or omit any part of the code.\n\n"
        f"Original {STATE.lang} code with polyglot conflicts:\n"
        f"{n}\n\n"
        f"Fully corrected {STATE.lang} code (all polyglot conflicts resolved, complete file):"
    )


def build_fix_prompt(code: str, analysis: str) -> str:
    n = _numbered(code)
    line_count = len(code.splitlines())
    return (
        f"You are a world-class {STATE.lang} engineer.\n\n"
        f"TASK: Rewrite the {STATE.lang} code so that EVERY defect identified in the analysis\n"
        f"below is completely and correctly fixed — including all polyglot syntax conflicts.\n\n"
        f"KNOWN ISSUES (fix every single one):\n"
        f"{analysis}\n\n"
        f"STRICT RULES:\n"
        f"  - Fix every syntax error, typo, logic error, import error, anti-pattern, and polyglot conflict.\n"
        f"  - Translate any foreign-language constructs into correct idiomatic {STATE.lang}.\n"
        f"  - Preserve the original intent and functionality exactly.\n"
        f"  - Use idiomatic {STATE.lang} throughout — no workarounds, no shortcuts.\n"
        f"  - All imports / requires / includes must be correct and present.\n"
        f"  - Every function body must be complete and correct.\n"
        f"  - Output ONLY the fixed source code.\n"
        f"  - Do NOT include markdown fences, backticks, commentary, or explanations.\n"
        f"  - Do NOT truncate, summarise, or omit any part of the code.\n"
        f"  - The output must be runnable as-is.\n\n"
        f"Original {STATE.lang} code ({line_count} lines):\n"
        f"{n}\n\n"
        f"Fixed {STATE.lang} code (complete, every line, no omissions):"
    )


def build_polyglot_verify_prompt(code: str) -> str:
    n = _numbered(code)
    line_count = len(code.splitlines())
    return (
        f"You are a strict {STATE.lang} syntax validator.\n\n"
        f"TASK: Inspect every line of the code below and determine whether any line\n"
        f"contains syntax from a language other than {STATE.lang}.\n\n"
        f"Foreign-language patterns to look for:\n"
        f"  - JavaScript: console.log(), var/let/const declarations, => arrow functions,\n"
        f"    require(), document.*, window.*\n"
        f"  - Java: System.out.print*(), public static void main, import java.*\n"
        f"  - Ruby: puts, end keyword as block terminator, .each do |x|\n"
        f"  - PHP: <?php, $variable syntax, echo statement\n"
        f"  - C/C++: #include <header>, printf(), cout <<, std::\n"
        f"  - Shell: #!/bin/bash, echo with quotes at line start\n\n"
        f"If NO foreign-language constructs are found, respond with exactly:\n"
        f"POLYGLOT CLEAN\n\n"
        f"If ANY foreign constructs remain, list each one:\n"
        f"POLYGLOT ISSUE: Line <N> — <foreign lang>: <offending text> — fix: <correct {STATE.lang}>\n"
        f"Then output the fully corrected code (complete, no markdown, no fences).\n\n"
        f"{STATE.lang} code to inspect ({line_count} lines):\n"
        f"{n}"
    )


def build_verify_prompt(original: str, fixed: str, pass_number: int) -> str:
    return (
        f"You are a senior {STATE.lang} code reviewer performing verification pass {pass_number} of {VERIFY_PASSES}.\n\n"
        f"Compare the ORIGINAL (buggy) code with the FIXED code.\n\n"
        f"VERIFY all of the following:\n"
        f"1. Every syntax error in the original is corrected.\n"
        f"2. No new bugs were introduced.\n"
        f"3. The fixed code is syntactically valid, runnable {STATE.lang}.\n"
        f"4. All imports / includes are correct and necessary.\n"
        f"5. All logic is preserved and correct.\n"
        f"6. NO foreign-language (polyglot) syntax remains — specifically check for:\n"
        f"   console.log(), System.out.print*(), puts, var/let/const declarations,\n"
        f"   $variables, echo, #include, cout, => arrow functions outside valid context.\n"
        f"7. No lines were accidentally omitted or truncated.\n\n"
        f"If the fixed code is completely correct on all 7 points, respond with exactly:\n"
        f"VERIFIED OK\n\n"
        f"If any issue remains, respond with:\n"
        f"ISSUE: <line number if visible> — <precise description>\n"
        f"Then output the fully corrected version (complete code, no omissions, no markdown).\n\n"
        f"ORIGINAL:\n"
        f"```\n"
        f"{original[:6000]}{'... [truncated for context]' if len(original) > 6000 else ''}\n"
        f"```\n\n"
        f"FIXED (pass {pass_number}):\n"
        f"```\n"
        f"{fixed[:6000]}{'... [truncated for context]' if len(fixed) > 6000 else ''}\n"
        f"```"
    )

# ─────────────────────────────────────────────────────────────────────────────
#  UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def extract_code(text: str) -> str:
    """Strip markdown fences if the model added them, return clean code."""
    match = re.search(r"```(?:[a-zA-Z0-9_+#.-]*)?\n?([\s\S]*?)```", text)
    if match:
        return match.group(1).strip()
    lines = text.strip().splitlines()
    if lines and re.match(
        r"^(Fixed|Corrected|Rewritten|Output|Result|Fully corrected|Complete)\s",
        lines[0], re.I,
    ):
        lines = lines[1:]
    return "\n".join(lines).strip()


def is_valid_extraction(candidate: str) -> bool:
    """Reject empty output and explicit model refusals, not legitimate rewrites."""
    candidate = candidate.strip()
    if not candidate:
        return False
    return not re.match(
        r"^(?:sorry[,! ]|i (?:cannot|can't|am unable to)|unable to)",
        candidate,
        re.IGNORECASE,
    )


def extract_bugs(analysis_text: str) -> list[str]:
    bugs: list[str] = []
    for line in analysis_text.split("\n"):
        stripped = line.strip()
        clean = re.sub(r"^\d+[.)]\s*", "", stripped).strip()
        if clean and 10 < len(clean) < 400 and not clean.lower().startswith("code"):
            bugs.append(clean[:200])
    result = bugs[:30]
    return result if result else ["Issues detected — see fixed code for all corrections."]


def extract_dependencies(analysis_text: str, code: str) -> list[str]:
    deps: set[str] = set()
    mgr = DEP_MANAGERS.get(STATE.lang or "")
    if not mgr:
        return []

    if STATE.lang == "Python":
        import_matches = re.findall(r"^(?:import|from)\s+([\w.]+)", code, re.MULTILINE)
        for imp in import_matches:
            top = imp.split(".")[0]
            if top and top not in PYTHON_STDLIB and not top.startswith("_"):
                deps.add(top)

    if STATE.lang in ("JavaScript", "TypeScript"):
        req_matches = re.findall(r"require\(['\"]([^'\"./][^'\"]*)['\"]", code)
        imp_matches = re.findall(r"from\s+['\"]([^'\"./][^'\"]*)['\"]", code)
        for pkg in req_matches + imp_matches:
            top = pkg.split("/")[0].lstrip("@")
            if top:
                deps.add(top)

    if STATE.lang in ("C", "C++"):
        inc_matches = re.findall(r'#include\s+[<"]([^>"]+)[>"]', code)
        system_headers = {
            "stdio", "stdlib", "string", "math", "assert", "ctype", "errno",
            "limits", "locale", "setjmp", "signal", "stdarg", "stddef", "time",
            "wchar", "wctype", "stdbool", "stdint", "inttypes", "complex",
            "fenv", "float", "iso646", "stdalign", "stdatomic", "stdnoreturn",
            "uchar", "algorithm", "bitset", "chrono", "cmath", "codecvt",
            "condition_variable", "deque", "exception", "forward_list",
            "fstream", "functional", "initializer_list", "iomanip", "ios",
            "iosfwd", "iostream", "istream", "iterator", "list", "map",
            "memory", "mutex", "new", "numeric", "ostream", "queue", "random",
            "ratio", "regex", "set", "sstream", "stack", "stdexcept",
            "streambuf", "system_error", "thread", "tuple", "type_traits",
            "typeindex", "typeinfo", "unordered_map", "unordered_set",
            "utility", "valarray", "vector",
        }
        for h in inc_matches:
            name = h.split(".")[0]
            if name not in system_headers:
                deps.add(h)

    pkg_mentions = re.findall(
        r"(?:install|missing|import|require|include|add|dependency|package)\s+"
        r"(?:the\s+)?[`'\"]?([a-zA-Z][a-zA-Z0-9_\-\.]+)[`'\"]?",
        analysis_text,
        re.IGNORECASE,
    )
    for p in pkg_mentions:
        if 2 < len(p) < 60:
            deps.add(p)

    return list(deps)[:15]


def show_diff(original: str, fixed: str) -> None:
    CONSOLE.print()
    CONSOLE.rule("[cyan]Unified Diff (original → fixed)[/cyan]", style="dim")
    orig_lines = original.splitlines(keepends=True)
    fixed_lines = fixed.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        orig_lines, fixed_lines,
        fromfile="original", tofile="fixed",
        lineterm="",
        n=2,
    ))
    if not diff:
        CONSOLE.print("  [dim]No differences detected.[/dim]")
        CONSOLE.print()
        return
    shown = 0
    for line in diff:
        shown += 1
        if shown > 120:
            CONSOLE.print(f"  [dim]… diff truncated ({len(diff) - shown} more lines)[/dim]")
            break
        line = line.rstrip("\n")
        if line.startswith("---") or line.startswith("+++"):
            CONSOLE.print(f"  [bold]{escape(line)}[/bold]")
        elif line.startswith("@@"):
            CONSOLE.print(f"  [cyan]{escape(line)}[/cyan]")
        elif line.startswith("+"):
            CONSOLE.print(f"  [green]{escape(line)}[/green]")
        elif line.startswith("-"):
            CONSOLE.print(f"  [red]{escape(line)}[/red]")
        else:
            CONSOLE.print(f"  [dim]{escape(line)}[/dim]")
    CONSOLE.print()

# ─────────────────────────────────────────────────────────────────────────────
#  DEPENDENCY INSTALL FLOW
# ─────────────────────────────────────────────────────────────────────────────

def prompt_deps_install(deps: list[str]) -> bool:
    mgr = DEP_MANAGERS.get(STATE.lang or "", {"cmd": "install"})
    CONSOLE.print()
    CONSOLE.print(Panel(
        "\n".join(f"  [yellow]{mgr['cmd']} {d}[/yellow]" for d in deps),
        title="[bold]Missing Dependencies Found[/bold]",
        subtitle="KodeDoc detected packages that may need installing",
        border_style="yellow",
    ))
    return Confirm.ask("  Install all?", default=True)


def run_install(deps: list[str]) -> None:
    """Attempt real installs for supported package managers; otherwise simulate."""
    mgr = DEP_MANAGERS.get(STATE.lang or "", {"cmd": "install"})
    real = False
    if STATE.lang == "Python":
        real = True
        base_cmd = [sys.executable, "-m", "pip", "install"]
    elif STATE.lang in ("JavaScript", "TypeScript"):
        real = True
        base_cmd = ["npm", "install"]
    else:
        base_cmd = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
        transient=True,
        console=CONSOLE,
    ) as progress:
        task = progress.add_task("Installing…", total=len(deps))
        for dep in deps:
            if real and base_cmd:
                CONSOLE.print(f"  [dim]$ {' '.join(base_cmd)} {dep}[/dim]")
                try:
                    r = subprocess.run(
                        base_cmd + [dep],
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    if r.returncode == 0:
                        CONSOLE.print(f"  [green]✔ {dep} installed[/green]")
                    else:
                        err = (r.stderr or r.stdout or "failed")[:120]
                        CONSOLE.print(f"  [yellow]⚠ {dep}: {escape(err)}[/yellow]")
                except Exception as exc:
                    CONSOLE.print(f"  [yellow]⚠ {dep}: {escape(str(exc))}[/yellow]")
            else:
                CONSOLE.print(f"  [dim]$ {mgr['cmd']} {dep}[/dim]")
                time.sleep(0.2)
                CONSOLE.print(f"  [green]✔ {dep} (recorded)[/green]")
            progress.advance(task)
    CONSOLE.print("  [green]✔ Dependency pass finished.[/green]")

# ─────────────────────────────────────────────────────────────────────────────
#  POLYGLOT REPAIR PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def repair_polyglot_conflicts(code: str, initial_conflicts: list) -> tuple[str, list, int]:
    working = code
    conflicts = initial_conflicts
    attempts = 0

    while conflicts and attempts < MAX_POLY_REPAIR_ATTEMPTS:
        attempts += 1
        CONSOLE.print(
            f"  [magenta]Repair attempt {attempts}/{MAX_POLY_REPAIR_ATTEMPTS} "
            f"— targeting {len(conflicts)} conflict(s)…[/magenta]"
        )

        prompt = build_polyglot_fix_prompt(working, conflicts)
        response = call_model(prompt, stream=False)

        if not response:
            CONSOLE.print("  [dim]Model unavailable — skipping this repair attempt.[/dim]")
            break

        candidate = extract_code(response)

        if not is_valid_extraction(candidate):
            CONSOLE.print(
                f"  [yellow]⚠ Attempt {attempts}: extraction too short or empty "
                f"({len(candidate)} chars vs {len(working)} original) — retrying.[/yellow]"
            )
            continue

        remaining = detect_polyglot_conflicts(candidate)
        resolved = len(conflicts) - len(remaining)
        if resolved > 0:
            CONSOLE.print(
                f"  [green]✔ Attempt {attempts}: resolved {resolved} conflict(s). "
                f"{len(remaining)} remaining.[/green]"
            )
        else:
            CONSOLE.print(
                f"  [yellow]⚠ Attempt {attempts}: no new conflicts resolved. "
                f"{len(remaining)} still present.[/yellow]"
            )

        working = candidate
        conflicts = remaining

        if not conflicts:
            break

        verify_response = call_model(build_polyglot_verify_prompt(working), stream=False)
        if verify_response and verify_response.strip().startswith("POLYGLOT CLEAN"):
            CONSOLE.print(
                "  [green]✔ Polyglot verify pass: POLYGLOT CLEAN — all conflicts resolved.[/green]"
            )
            conflicts = []
            break
        elif verify_response and "POLYGLOT ISSUE" in verify_response.upper():
            second = extract_code(verify_response)
            if is_valid_extraction(second):
                working = second
                conflicts = detect_polyglot_conflicts(working)
                CONSOLE.print(
                    f"  [cyan]Polyglot verify pass applied a correction. "
                    f"{len(conflicts)} conflict(s) remaining.[/cyan]"
                )

    return working, conflicts, attempts

# ─────────────────────────────────────────────────────────────────────────────
#  OLLAMA MODEL PICKER
# ─────────────────────────────────────────────────────────────────────────────

def fetch_cloud_models() -> list[dict[str, str]]:
    try:
        resp = requests.get(f"{STATE.ollama_url.rstrip('/')}/api/tags", timeout=8)
        resp.raise_for_status()
        data = resp.json()
        raw_models = data.get("models", [])
        if not raw_models:
            CONSOLE.print("  [yellow]Ollama is running but no models are installed.[/yellow]")
            CONSOLE.print("  [dim]Pull a model first: [bold]ollama pull codellama:7b[/bold][/dim]")
            return []
        models = []
        for m in raw_models:
            name = m.get("name", "")
            if not name:
                continue
            size_gb = f"{m['size'] / 1e9:.1f} GB" if m.get("size") else ""
            family = m.get("details", {}).get("family", "")
            modified = ""
            if m.get("modified_at"):
                try:
                    modified = f"last updated {m['modified_at'][:10]}"
                except Exception:
                    pass
            parts = [p for p in [family, size_gb, modified] if p]
            models.append({
                "name": name,
                "desc": " · ".join(parts) if parts else "Local model",
                "tag": "AVAILABLE",
            })
        return models
    except requests.exceptions.ConnectionError:
        CONSOLE.print(f"  [red]✖ Cannot reach {STATE.ollama_url}.[/red]")
        CONSOLE.print("  [dim]Make sure Ollama is running: [bold]ollama serve[/bold][/dim]")
        return []
    except Exception as exc:
        CONSOLE.print(f"  [red]✖ Endpoint query failed: {escape(str(exc))}[/red]")
        return []


def select_model_flow() -> None:
    CONSOLE.print()
    CONSOLE.print(Panel(
        "[bold white]Ollama Model Picker[/bold white]\n"
        "[dim]Fetching models available on your Ollama instance…[/dim]",
        border_style="cyan", padding=(0, 2),
    ))
    CONSOLE.print(f"  [dim]Querying {STATE.ollama_url}…[/dim]")
    models = fetch_cloud_models()
    if not models:
        CONSOLE.print("[red]  No models could be listed.[/red]")
        return

    table = Table(
        show_header=True, header_style="bold cyan",
        box=box.SIMPLE_HEAVY, border_style="dim", show_lines=False,
    )
    table.add_column("#", style="dim", width=4)
    table.add_column("Model", style="bold white", min_width=28)
    table.add_column("Details", style="dim")
    table.add_column("Tag", style="bold green", width=14)
    for i, m in enumerate(models, 1):
        table.add_row(str(i), m["name"], m["desc"], m["tag"])

    CONSOLE.print()
    CONSOLE.print(table)
    CONSOLE.print(f"  [dim]{len(models)} model(s) available[/dim]\n")

    choice_str = Prompt.ask("  Select model number", default="1", console=CONSOLE).strip()
    try:
        idx = int(choice_str) - 1
        assert 0 <= idx < len(models)
    except (ValueError, AssertionError):
        CONSOLE.print("[red]  Invalid selection.[/red]")
        return

    STATE.model = models[idx]["name"]
    CONSOLE.print(f"  [dim]Verifying connection to '{STATE.model}'…[/dim]")
    try:
        probe = requests.post(
            f"{STATE.ollama_url.rstrip('/')}/api/generate",
            json={
                "model": STATE.model,
                "prompt": "Say 'OK' in one word.",
                "stream": False,
                "options": {"num_predict": 4, "temperature": 0.0},
            },
            timeout=20,
        )
        probe.raise_for_status()
        CONSOLE.print(f"  [green]✔ Connected → {STATE.model}[/green]")
        CONSOLE.print("  [cyan]Ready. Select a language and load your code.[/cyan]")
    except Exception:
        CONSOLE.print(f"  [yellow]⚠ Could not verify '{STATE.model}' — model is set anyway.[/yellow]")
        CONSOLE.print("  [dim]If debugging fails, check the URL and model name.[/dim]")

# ─────────────────────────────────────────────────────────────────────────────
#  LANGUAGE / URL
# ─────────────────────────────────────────────────────────────────────────────

def select_language_flow() -> None:
    table = Table(show_header=False, box=box.SIMPLE, border_style="dim", padding=(0, 1))
    cols = 6
    for _ in range(cols):
        table.add_column()
    for i in range(0, len(LANGUAGES), cols):
        row = LANGUAGES[i:i + cols]
        padded = row + [""] * (cols - len(row))
        styled = [
            f"[bold green]{lang}[/bold green]" if lang == STATE.lang
            else (f"[dim]{lang}[/dim]" if lang else "")
            for lang in padded
        ]
        table.add_row(*styled)
    CONSOLE.print()
    CONSOLE.print(table)
    lang_input = Prompt.ask("  Language", default=STATE.lang or "Python", console=CONSOLE).strip()
    match = next((l for l in LANGUAGES if l.lower() == lang_input.lower()), None)
    if not match:
        CONSOLE.print(f"  [red]'{escape(lang_input)}' is not in the supported list.[/red]")
        return
    STATE.lang = match
    CONSOLE.print(f"  [green]Language set → {STATE.lang}[/green]")


def set_url_flow() -> None:
    CONSOLE.print()
    current = STATE.ollama_url
    CONSOLE.print(f"  [dim]Current Ollama URL: {current}[/dim]")
    new = Prompt.ask("  New URL", default=current, console=CONSOLE).strip()
    if not new:
        return
    if not new.startswith(("http://", "https://")):
        CONSOLE.print("  [red]URL must start with http:// or https://[/red]")
        return
    STATE.ollama_url = new.rstrip("/")
    CONSOLE.print(f"  [green]Ollama URL → {STATE.ollama_url}[/green]")

# ─────────────────────────────────────────────────────────────────────────────
#  FILE LOAD — up to 1 GB
# ─────────────────────────────────────────────────────────────────────────────

def load_file_from_device() -> str:
    CONSOLE.print()
    CONSOLE.print(
        "  [dim]Enter the path to the file on your device.\n"
        "  Absolute or relative paths accepted. Max local file size: 1 GB.\n"
        "  The AI model has separate input/output limits; a 1 GB source file\n"
        "  cannot be processed as one model request.[/dim]"
    )
    file_path = Prompt.ask("  File path", console=CONSOLE).strip()
    if not file_path:
        CONSOLE.print("  [yellow]No path entered.[/yellow]")
        return ""

    file_path = os.path.expanduser(os.path.expandvars(file_path))

    if not os.path.isfile(file_path):
        CONSOLE.print(f"  [red]✖ File not found: {escape(file_path)}[/red]")
        return ""

    try:
        file_size = os.path.getsize(file_path)
    except OSError as exc:
        CONSOLE.print(f"  [red]✖ Cannot stat file: {escape(str(exc))}[/red]")
        return ""

    if file_size == 0:
        CONSOLE.print("  [yellow]File is empty.[/yellow]")
        return ""

    if file_size > MAX_LOAD_BYTES:
        gb = file_size / (1024 ** 3)
        CONSOLE.print(f"  [red]✖ File is {gb:.2f} GB — exceeds the 1 GB limit.[/red]")
        return ""

    chunk_size = 8 * 1024 * 1024
    raw_data = bytearray()
    bytes_read = 0

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            transient=True,
            console=CONSOLE,
        ) as progress:
            task = progress.add_task("Reading file…", total=file_size)
            with open(file_path, "rb") as fh:
                while True:
                    data = fh.read(chunk_size)
                    if not data:
                        break
                    raw_data.extend(data)
                    bytes_read += len(data)
                    progress.update(task, advance=len(data))
    except OSError as exc:
        CONSOLE.print(f"  [red]✖ Read error: {escape(str(exc))}[/red]")
        return ""

    try:
        content = raw_data.decode("utf-8")
    except UnicodeDecodeError:
        content = raw_data.decode("latin-1")

    line_count = content.count("\n") + 1
    mb = bytes_read / (1024 * 1024)
    filename = os.path.basename(file_path)

    CONSOLE.print(
        f"  [green]✔ Loaded '{escape(filename)}' — {line_count:,} lines · {mb:.2f} MB[/green]"
    )

    if not STATE.lang:
        ext = os.path.splitext(file_path)[1].lower()
        detected = EXT_TO_LANG.get(ext) or EXT_TO_LANG.get(os.path.splitext(file_path)[1])
        if detected:
            STATE.lang = detected
            CONSOLE.print(f"  [dim]Language auto-detected → {STATE.lang} (from {ext})[/dim]")

    return content

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN DEBUG PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def run_debug(code: str) -> None:
    if STATE.running:
        CONSOLE.print("  [yellow]Already running.[/yellow]")
        return
    if not code.strip():
        CONSOLE.print("  [red]No code to debug.[/red]")
        return
    if not STATE.lang:
        CONSOLE.print("  [red]No language selected. Use 'lang' command.[/red]")
        return
    if not STATE.model:
        CONSOLE.print("  [red]No model selected. Use 'model' command.[/red]")
        return

    STATE.running = True

    line_count = len(code.splitlines())
    char_count = len(code)

    if char_count > SOFT_CHAR_WARN:
        CONSOLE.print(
            f"  [yellow]⚠ Code is {char_count:,} chars — large prompts may hit "
            f"model context limits. If supported by your model, raise "
            f"KODEDOC_CONTEXT_TOKENS and KODEDOC_GENERATION_TOKENS before launch.[/yellow]"
        )

    CONSOLE.print()
    CONSOLE.rule(style="dim")
    CONSOLE.print(f"  [dim]Model:      {STATE.model}[/dim]")
    CONSOLE.print(f"  [dim]Language:   {STATE.lang}[/dim]")
    CONSOLE.print(f"  [dim]Lines:      {line_count:,}[/dim]")
    CONSOLE.print(f"  [dim]Size:       {char_count:,} chars[/dim]")
    CONSOLE.print(
        f"  [dim]Requested model limits: {MODEL_CONTEXT_TOKENS:,} context / "
        f"{MAX_GENERATION_TOKENS:,} output tokens[/dim]"
    )
    CONSOLE.print(f"  [dim]Verify:     {VERIFY_PASSES}× passes[/dim]")
    CONSOLE.rule(style="dim")
    CONSOLE.print()

    # Step 1: Deep analysis
    CONSOLE.print("[bold cyan]● Step 1 — Deep analysis (scanning every line)…[/bold cyan]")
    analysis = call_model(build_analysis_prompt(code), stream=False)
    if not analysis:
        CONSOLE.print("  [red]✖ Analysis failed.[/red]")
        STATE.running = False
        return

    bugs = extract_bugs(analysis)
    CONSOLE.print(f"  [yellow]{len(bugs)} issue(s) detected:[/yellow]")
    for b in bugs:
        CONSOLE.print(f"  [yellow]◆ {escape(b)}[/yellow]")
    CONSOLE.print()

    # Step 1b: Static polyglot detection
    CONSOLE.print("[bold cyan]● Step 1b — Static polyglot conflict scan…[/bold cyan]")
    initial_conflicts = detect_polyglot_conflicts(code)
    if initial_conflicts:
        CONSOLE.print(f"  [magenta]{len(initial_conflicts)} polyglot conflict(s) detected:[/magenta]")
        for c in initial_conflicts:
            CONSOLE.print(
                f"  [magenta]◆ Line {c['line_no']} [{c['secondary_lang']}]: "
                f"{escape(c['line_text'][:80])}[/magenta]"
            )
    else:
        CONSOLE.print("  [green]✔ No static polyglot conflicts detected.[/green]")
    CONSOLE.print()

    # Step 2: Dependencies
    CONSOLE.print("[bold cyan]● Step 2 — Scanning for missing dependencies…[/bold cyan]")
    deps = extract_dependencies(analysis, code)
    if deps:
        CONSOLE.print(f"  [yellow]Found {len(deps)} potential package(s): {', '.join(deps)}[/yellow]")
        if STATE.auto_install == "always":
            run_install(deps)
        elif STATE.auto_install == "ask":
            if prompt_deps_install(deps):
                run_install(deps)
            else:
                CONSOLE.print("  [dim]Dependencies skipped. Code will still be fixed.[/dim]")
        else:
            CONSOLE.print("  [dim]Auto-install disabled. Skipping.[/dim]")
    else:
        CONSOLE.print("  [green]✔ No missing dependencies detected.[/green]")
    CONSOLE.print()

    # Step 2b: Polyglot repair loop
    working_code = code
    final_conflicts = initial_conflicts

    if initial_conflicts:
        CONSOLE.print(
            f"[bold cyan]● Step 2b — Polyglot repair loop "
            f"(up to {MAX_POLY_REPAIR_ATTEMPTS} attempts)…[/bold cyan]"
        )
        repaired, final_conflicts, attempts_used = repair_polyglot_conflicts(
            code, initial_conflicts
        )

        if not final_conflicts:
            working_code = repaired
            CONSOLE.print(
                f"  [green]✔ All {len(initial_conflicts)} polyglot conflict(s) resolved "
                f"in {attempts_used} attempt(s).[/green]"
            )
        else:
            working_code = repaired
            CONSOLE.print(
                f"  [yellow]⚠ {len(final_conflicts)} conflict(s) still present after "
                f"{attempts_used} attempt(s) — passing to fix step.[/yellow]"
            )
            for c in final_conflicts:
                CONSOLE.print(
                    f"  [yellow]  Remaining: Line {c['line_no']} [{c['secondary_lang']}]: "
                    f"{escape(c['line_text'][:80])}[/yellow]"
                )
        CONSOLE.print()

    # Step 3: Full fix rewrite
    CONSOLE.print("[bold cyan]● Step 3 — Rewriting and fixing all bugs…[/bold cyan]")
    fix_response = call_model(build_fix_prompt(working_code, analysis), stream=True)
    if not fix_response:
        CONSOLE.print("  [red]✖ Fix pass failed.[/red]")
        STATE.running = False
        return

    fixed = extract_code(fix_response)
    if not is_valid_extraction(fixed):
        CONSOLE.print(
            "  [red]✖ The model returned no usable code (it may have refused "
            "the request). No output was saved.[/red]"
        )
        STATE.running = False
        return

    # Step 4: Verification loop
    CONSOLE.print()
    CONSOLE.print(f"[bold cyan]● Step 4 — {VERIFY_PASSES}× Verification passes…[/bold cyan]")

    pass_verdicts: list[str] = []
    current = fixed

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        transient=False,
        console=CONSOLE,
    ) as progress:
        vtask = progress.add_task("Verifying…", total=VERIFY_PASSES)

        for pass_num in range(1, VERIFY_PASSES + 1):
            progress.update(vtask, description=f"Pass {pass_num}/{VERIFY_PASSES}…")
            verify_response = call_model(
                build_verify_prompt(working_code, current, pass_num),
                stream=False,
            )

            if not verify_response:
                verdict = "SKIPPED"
                pass_verdicts.append(verdict)
                CONSOLE.print(f"  [dim]Pass {pass_num}: skipped (model unavailable)[/dim]")
            elif verify_response.strip().startswith("VERIFIED OK"):
                verdict = "OK"
                pass_verdicts.append(verdict)
                CONSOLE.print(f"  [green]✔ Pass {pass_num}: VERIFIED OK[/green]")
            elif "ISSUE:" in verify_response.upper():
                verdict = "FIXED"
                second = extract_code(verify_response)
                if is_valid_extraction(second):
                    current = second
                    CONSOLE.print(f"  [yellow]⚠ Pass {pass_num}: Issues found — applied correction.[/yellow]")
                else:
                    CONSOLE.print(f"  [yellow]⚠ Pass {pass_num}: Issues found — correction extraction failed.[/yellow]")
                pass_verdicts.append(verdict)
            else:
                verdict = "UNCLEAR"
                pass_verdicts.append(verdict)
                CONSOLE.print(f"  [dim]Pass {pass_num}: Response unclear — holding current fix.[/dim]")

            progress.advance(vtask)

    fixed = current
    clean_passes = pass_verdicts.count("OK")
    CONSOLE.print()
    CONSOLE.print(
        f"  [bold]Verification summary:[/bold] "
        f"[green]{clean_passes}/{VERIFY_PASSES} clean[/green]  |  "
        f"Verdicts: {' → '.join(pass_verdicts)}"
    )
    if clean_passes >= 3:
        CONSOLE.print("  [green]✔ Code cleared by majority verification.[/green]")
    elif clean_passes >= 1:
        CONSOLE.print("  [yellow]⚠ Partial verification — review the output carefully.[/yellow]")
    else:
        CONSOLE.print("  [red]⚠ No clean pass achieved — output may still contain issues.[/red]")

    # Final polyglot spot-check
    final_poly_check = detect_polyglot_conflicts(fixed)
    if final_poly_check:
        CONSOLE.print(
            f"  [red]⚠ Post-fix polyglot scan: {len(final_poly_check)} construct(s) "
            f"may still be present — review manually.[/red]"
        )
        for c in final_poly_check:
            CONSOLE.print(
                f"  [red]  Line {c['line_no']} [{c['secondary_lang']}]: "
                f"{escape(c['line_text'][:80])}[/red]"
            )
    else:
        CONSOLE.print("  [green]✔ Post-fix polyglot scan: clean.[/green]")

    # Display result
    STATE.last_fixed = fixed
    CONSOLE.print()
    try:
        CONSOLE.print(Panel(
            Syntax(fixed, (STATE.lang or "text").lower(), theme="monokai", line_numbers=True),
            title="[bold green]Fixed Code[/bold green]",
            border_style="green",
        ))
    except Exception:
        CONSOLE.print(Panel(fixed, title="[bold green]Fixed Code[/bold green]", border_style="green"))

    if STATE.diff_mode and fixed:
        show_diff(code, fixed)

    orig_lines = len(code.splitlines())
    fixed_lines = len(fixed.splitlines())

    CONSOLE.print()
    CONSOLE.rule(style="dim")
    CONSOLE.print("[bold green]✔ Debug complete.[/bold green]")
    CONSOLE.print(f"  [dim]Original:           {orig_lines:,} lines[/dim]")
    CONSOLE.print(f"  [dim]Fixed:              {fixed_lines:,} lines[/dim]")
    CONSOLE.print(f"  [dim]Polyglot detected:  {len(initial_conflicts)}[/dim]")
    CONSOLE.print(f"  [dim]Polyglot remaining: {len(final_poly_check)}[/dim]")
    CONSOLE.print(f"  [dim]Bugs found:         {len(bugs)}[/dim]")
    CONSOLE.print(f"  [dim]Verify passes:      {clean_passes}/{VERIFY_PASSES} clean[/dim]")

    # Session log — metadata + short samples only (memory safe)
    STATE.session_log.append({
        "ts": datetime.datetime.now().isoformat(),
        "lang": STATE.lang,
        "model": STATE.model,
        "original_lines": orig_lines,
        "fixed_lines": fixed_lines,
        "bugs_count": len(bugs),
        "bugs_sample": bugs[:5],
        "polyglot_initial": len(initial_conflicts),
        "polyglot_remaining": len(final_poly_check),
        "verify_verdicts": pass_verdicts,
        "clean_passes": clean_passes,
    })

    STATE.running = False

# ─────────────────────────────────────────────────────────────────────────────
#  EXPORT / COPY
# ─────────────────────────────────────────────────────────────────────────────

def export_output() -> None:
    if not STATE.last_fixed:
        CONSOLE.print("  [red]Nothing to export yet. Run a debug session first.[/red]")
        return
    ext = DEP_MANAGERS.get(STATE.lang or "", {}).get("ext", ".txt")
    filename = f"fixed_code{ext}"
    out_path = Prompt.ask("  Save as", default=filename, console=CONSOLE).strip()
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(STATE.last_fixed)
        CONSOLE.print(f"  [green]✔ Exported to {escape(out_path)}[/green]")
    except OSError as e:
        CONSOLE.print(f"  [red]✖ Export failed: {escape(str(e))}[/red]")


def copy_output() -> None:
    if not STATE.last_fixed:
        CONSOLE.print("  [red]Nothing to copy yet. Run a debug session first.[/red]")
        return
    text = STATE.last_fixed.encode()
    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            proc.communicate(text)
            CONSOLE.print("  [green]✔ Copied to clipboard[/green]")
            return
        except FileNotFoundError:
            continue
    try:
        import pyperclip  # type: ignore
        pyperclip.copy(STATE.last_fixed)
        CONSOLE.print("  [green]✔ Copied to clipboard[/green]")
    except Exception:
        CONSOLE.print("  [yellow]Clipboard not available — use 'export' to save to a file.[/yellow]")

# ─────────────────────────────────────────────────────────────────────────────
#  EDITOR / CLIPBOARD HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def open_editor() -> str:
    editor = os.environ.get("EDITOR", "nano")
    suffix = DEP_MANAGERS.get(STATE.lang or "", {}).get("ext", ".txt")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, mode="w") as tf:
        tf_name = tf.name
    os.system(f"{editor} {tf_name}")
    try:
        with open(tf_name, "r", encoding="utf-8") as f:
            content = f.read()
        os.unlink(tf_name)
        return content
    except OSError:
        return ""


def paste_from_clipboard() -> str:
    for cmd in (
        ["pbpaste"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
    ):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout
        except FileNotFoundError:
            continue
    try:
        import pyperclip  # type: ignore
        return pyperclip.paste()
    except Exception:
        return ""

# ─────────────────────────────────────────────────────────────────────────────
#  SESSION LOG / STATUS
# ─────────────────────────────────────────────────────────────────────────────

def show_history() -> None:
    if not STATE.session_log:
        CONSOLE.print("  [dim]No sessions yet. Debug some code first.[/dim]")
        return
    CONSOLE.print(f"\n  [cyan]Session History — {len(STATE.session_log)} session(s)[/cyan]\n")
    for i, s in enumerate(STATE.session_log, 1):
        clean = s.get("clean_passes", "?")
        poly_in = s.get("polyglot_initial", 0)
        poly_out = s.get("polyglot_remaining", 0)
        verdicts = " → ".join(s.get("verify_verdicts", []))
        CONSOLE.print(f"  [dim]──── Session {i} ──────────────────────────────[/dim]")
        CONSOLE.print(f"  [dim]Time:              {s['ts']}[/dim]")
        CONSOLE.print(f"  [dim]Lang:              {s['lang']}[/dim]")
        CONSOLE.print(f"  [dim]Model:             {s['model']}[/dim]")
        CONSOLE.print(f"  [dim]Bugs found:        {s.get('bugs_count', 0)}[/dim]")
        CONSOLE.print(f"  [dim]Polyglot detected: {poly_in} conflict(s)[/dim]")
        CONSOLE.print(f"  [dim]Polyglot remaining:{poly_out}[/dim]")
        CONSOLE.print(f"  [dim]Verify passes:     {clean}/{VERIFY_PASSES} clean[/dim]")
        CONSOLE.print(f"  [dim]Verdicts:          {verdicts}[/dim]")
        CONSOLE.print(f"  [green]Status:            Fixed[/green]")
        CONSOLE.print()


def print_status() -> None:
    model_str = f"[green]{STATE.model}[/green]" if STATE.model else "[red]no model[/red]"
    lang_str = f"[cyan]{STATE.lang}[/cyan]" if STATE.lang else "[red]no lang[/red]"
    diff_str = "[green]ON[/green]" if STATE.diff_mode else "[dim]OFF[/dim]"
    auto_str = f"[yellow]{STATE.auto_install.upper()}[/yellow]"
    CONSOLE.print(
        f"  model={model_str}  lang={lang_str}  "
        f"diff={diff_str}  auto-install={auto_str}"
    )


def show_full_status() -> None:
    CONSOLE.print()
    CONSOLE.print(Panel(
        f"[bold]KodeDoc v{VERSION}[/bold]\n"
        f"Ollama URL : {STATE.ollama_url}\n"
        f"Model      : {STATE.model or '(none)'}\n"
        f"Language   : {STATE.lang or '(none)'}\n"
        f"Diff mode  : {'ON' if STATE.diff_mode else 'OFF'}\n"
        f"Auto-install: {STATE.auto_install}\n"
        f"Sessions   : {len(STATE.session_log)}\n"
        f"Last fixed : {len(STATE.last_fixed.splitlines()) if STATE.last_fixed else 0} lines",
        title="Status",
        border_style="cyan",
    ))

# ─────────────────────────────────────────────────────────────────────────────
#  HELP
# ─────────────────────────────────────────────────────────────────────────────

HELP_TABLE = [
    ("model", "Pick a model from your Ollama instance"),
    ("lang", "Select a programming language (30 supported)"),
    ("url", "Set Ollama base URL (default http://localhost:11434)"),
    ("load", "Load a local file (up to 1 GB; AI model limits still apply)"),
    ("paste", "Paste code from clipboard into the buffer"),
    ("edit", "Open $EDITOR to write/paste code manually"),
    ("sample", "Load a buggy example for the selected language"),
    ("debug", "Run the full pipeline (analysis → polyglot repair → fix → 5× verify)"),
    ("copy", "Copy fixed code to clipboard"),
    ("export", "Save fixed code to a file"),
    ("diff", "Toggle unified diff view"),
    ("auto", "Cycle auto-install: ask → always → never"),
    ("clear", "Reset the code buffer"),
    ("show", "Display the current code buffer with syntax highlighting"),
    ("history", "Show past debug sessions (metadata)"),
    ("status", "Full status dump"),
    ("help", "Show this reference"),
    ("quit / exit", "Exit KodeDoc"),
]


def show_help() -> None:
    table = Table(
        show_header=True, header_style="bold cyan",
        box=box.SIMPLE, border_style="dim", expand=False,
    )
    table.add_column("Command", style="bold magenta", min_width=14)
    table.add_column("Description", style="dim white")
    for cmd, desc in HELP_TABLE:
        table.add_row(cmd, desc)
    CONSOLE.print()
    CONSOLE.print(Panel(
        table,
        title=f"[bold]KodeDoc v{VERSION} Command Reference[/bold]",
        border_style="cyan",
    ))
    langs_str = " · ".join(LANGUAGES)
    CONSOLE.print(f"  [dim]Supported languages ({len(LANGUAGES)}): {langs_str}[/dim]\n")

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN REPL
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    CONSOLE.print(Panel(
        Text.assemble(
            ("KodeDoc", "bold white"),
            (" — AI Debug Agent", "bold cyan"),
            ("\n", ""),
            (
                f"v{VERSION}  ·  Ollama + Claude fallback  ·  "
                f"{len(LANGUAGES)} languages  ·  1 GB local load limit  ·  "
                f"Polyglot repair loop  ·  {VERIFY_PASSES}× verify",
                "dim",
            ),
        ),
        border_style="cyan",
        padding=(0, 2),
    ))
    CONSOLE.print()
    CONSOLE.print(
        "  [dim]Type [bold]help[/bold] for command reference.  "
        "[bold]quit[/bold] to exit.[/dim]"
    )
    CONSOLE.print()

    code_buffer: str = ""

    while True:
        try:
            print_status()
            raw = Prompt.ask("\n  [bold cyan]>[/bold cyan]", console=CONSOLE).strip()
        except (KeyboardInterrupt, EOFError):
            CONSOLE.print("\n  [dim]Bye.[/dim]")
            break

        if not raw:
            continue

        cmd = raw.lower()

        if cmd in ("quit", "exit", "q"):
            CONSOLE.print("  [dim]Bye.[/dim]")
            break

        elif cmd in ("help", "?", "h"):
            show_help()

        elif cmd == "model":
            select_model_flow()

        elif cmd in ("lang", "language"):
            select_language_flow()

        elif cmd == "url":
            set_url_flow()

        elif cmd == "load":
            text = load_file_from_device()
            if text.strip():
                code_buffer = text
            else:
                CONSOLE.print("  [yellow]File load returned no content. Buffer unchanged.[/yellow]")

        elif cmd == "sample":
            if not STATE.lang:
                CONSOLE.print("  [red]Select a language first.[/red]")
                continue
            sample = SAMPLES.get(STATE.lang)
            if sample:
                code_buffer = sample
                CONSOLE.print(
                    f"  [green]Sample {STATE.lang} code loaded (contains intentional bugs).[/green]"
                )
            else:
                code_buffer = (
                    f"# Sample {STATE.lang} code with intentional bugs\n"
                    f"# Paste or load your real code to debug it\n"
                )
                CONSOLE.print(
                    f"  [yellow]No sample for {STATE.lang}. Generic template loaded.[/yellow]"
                )

        elif cmd == "paste":
            text = paste_from_clipboard()
            if text:
                code_buffer = text
                CONSOLE.print(
                    f"  [green]✔ Pasted {len(text.splitlines())} lines from clipboard.[/green]"
                )
            else:
                CONSOLE.print(
                    "  [yellow]Clipboard empty or not accessible. Try 'load' or 'edit'.[/yellow]"
                )

        elif cmd == "edit":
            CONSOLE.print("  [dim]Opening editor… save and close to continue.[/dim]")
            text = open_editor()
            if text.strip():
                code_buffer = text
                CONSOLE.print(
                    f"  [green]✔ Loaded {len(text.splitlines())} lines from editor.[/green]"
                )
            else:
                CONSOLE.print("  [yellow]Editor returned empty content. Buffer unchanged.[/yellow]")

        elif cmd == "debug":
            if not code_buffer.strip():
                CONSOLE.print(
                    "  [red]Buffer is empty. Use 'load', 'sample', 'paste', or 'edit' first.[/red]"
                )
            else:
                run_debug(code_buffer)

        elif cmd == "copy":
            copy_output()

        elif cmd == "export":
            export_output()

        elif cmd == "diff":
            STATE.diff_mode = not STATE.diff_mode
            status = "[green]enabled[/green]" if STATE.diff_mode else "[dim]disabled[/dim]"
            CONSOLE.print(f"  Diff view: {status}")

        elif cmd == "auto":
            states = ["ask", "always", "never"]
            idx = states.index(STATE.auto_install)
            STATE.auto_install = states[(idx + 1) % 3]
            CONSOLE.print(f"  Auto-install: [yellow]{STATE.auto_install.upper()}[/yellow]")

        elif cmd == "clear":
            code_buffer = ""
            STATE.last_fixed = ""
            CONSOLE.print("  [dim]Buffer cleared.[/dim]")

        elif cmd in ("history", "log", "sessions"):
            show_history()

        elif cmd == "status":
            show_full_status()

        elif cmd in ("show", "view", "code"):
            if code_buffer.strip():
                try:
                    CONSOLE.print(Panel(
                        Syntax(
                            code_buffer,
                            (STATE.lang or "text").lower(),
                            theme="monokai",
                            line_numbers=True,
                        ),
                        title="[dim]Current Buffer[/dim]",
                        border_style="dim",
                    ))
                except Exception:
                    CONSOLE.print(
                        Panel(code_buffer, title="[dim]Current Buffer[/dim]", border_style="dim")
                    )
            else:
                CONSOLE.print("  [dim]Buffer is empty.[/dim]")

        else:
            CONSOLE.print(
                f"  [red]Unknown command:[/red] [dim]{escape(raw)}[/dim]"
                f"  — type [bold]help[/bold]"
            )


if __name__ == "__main__":
    main()
