# KodeDoc — Autonomous AI Debug Agent

KodeDoc is an advanced, local-first AI debugging agent that doesn't just analyze your code—it **executes** it, finds the errors, fixes them, and writes unit tests to prove it works. 

Built for developers who want a powerful, privacy-focused assistant that runs entirely on their own hardware using Ollama.

## 🚀 Features

- **Autonomous Execution Loop:** KodeDoc writes the fixed code to a temporary file, runs it in your terminal, catches the `Traceback` or `SyntaxError`, feeds it back to the AI, and forces it to fix its own mistakes.
- **Automated Unit Testing:** After fixing your code, KodeDoc generates a comprehensive suite of `pytest` unit tests, runs them, and reports which edge cases pass or fail.
- **Polyglot Contamination Repair:** If you have JavaScript `console.log()` inside a Python file, or Java `System.out.println()` inside a C++ file, KodeDoc detects the foreign syntax and translates it into the correct language.
- **Universal Language Support:** Debug and convert over 30 programming languages.
- **Language Conversion:** Translate a Python script into Rust, Go, or C++ with a single command.
- **Dependency Auto-Installer:** Scans your imports and automatically installs missing `pip`, `npm`, or `gem` packages before running the code.
- **Interactive Chat Mode:** Talk directly to your local AI model inside the terminal to brainstorm solutions or ask coding questions without leaving your workflow.
- **Windows/WSL Clipboard Bridge:** A built-in `copy` command that instantly pushes the fixed code to your Windows clipboard (even from inside WSL) so you can paste it straight into your editor.
- **Total Privacy:** Zero cloud APIs required. Connects directly to your local Ollama instance.

---

## 🛠️ Installation

### 1. Requirements
- Python 3.9+
- [Ollama](https://ollama.com/) installed and running (`ollama serve`).
- A capable coding model pulled locally. We highly recommend `qwen2.5-coder` or `dolphin-mixtral`.
  ```bash
  ollama pull qwen2.5-coder:14b
  ```

### 2. Setup
Clone or copy the `Kode_Doc.py` script into your working directory. KodeDoc automatically installs its own Python dependencies (`requests`, `rich`, `pyperclip`) on the first run.

---

## 🕹️ Usage

Launch the agent:
```bash
python3 Kode_Doc.py
```

### Basic Workflow:
1. **`model`** — Select your Ollama model.
2. **`lang`** — Select the programming language (a numbered grid will pop up).
3. **`load`** — Enter the path to your buggy file (e.g., `/home/user/project/main.py`).
4. **`debug`** — Run the full autonomous pipeline:
   - Analyzes the code.
   - Fixes dependencies.
   - Translates polyglot conflicts.
   - Rewrites the code.
   - Executes it in the terminal and auto-fixes crashes.
   - Generates and runs unit tests.
5. **`copy`** — Instantly copy the perfect, working code to your clipboard.

---

## 📖 Command Reference

| Command | Description |
| :--- | :--- |
| `help` | Show the command menu |
| `model` | Pick an Ollama model from your local instance |
| `lang` | Select a programming language from a visual grid |
| `load` | Load a local source file (up to 1 GB) |
| `debug` | Run the full pipeline (Analysis -> Deps -> Fix -> Execute -> Test) |
| `convert` | Translate the current code into a different language |
| `chat` | Enter interactive AI chat mode (type `exit` to leave) |
| `copy` | Copy the fixed code to the clipboard (WSL compatible) |
| `diff` | Toggle the unified diff view to see what was changed |
| `clear` | Reset the code buffer |
| `quit` | Exit KodeDoc |

---

## 🧠 How it Works (The Pipeline)

1. **Analysis:** The AI reads the code and lists every bug, typo, logic error, and foreign-language token.
2. **Dependency Check:** KodeDoc scans the imports, extracts the package names, and runs `pip install` (or `npm install`) automatically.
3. **The Fix:** The AI rewrites the file based on the analysis.
4. **The Execution Loop (Agentic):** KodeDoc saves the fixed code to a temp file and runs it. If it crashes, it hands the terminal error back to the AI and says, *"Fix this error."* It loops up to 3 times.
5. **Unit Testing:** It asks the AI to write a test file, runs `pytest`, and reports the pass/fail status.
6. **Verification:** It runs a final syntax check to ensure no foreign-language tokens snuck through.

---

## ⚠️ Notes & Limitations

- **Context Limits:** If you load a 10,000-line file, the local model's context window will overflow. KodeDoc warns you if the code exceeds 80,000 characters. For massive files, break them into smaller modules.
- **Model Choice:** Small models (like `llama3:8b` or `codellama:7b`) are terrible at following strict formatting rules. Use `qwen2.5-coder` or `dolphin-mixtral` for the best results.
- **Sandboxing:** When running the execution loop, the code is executed directly on your machine. Ensure the code is from a trusted source or run KodeDoc inside a virtual machine or Docker container when debugging untrusted scripts.
```

### How to save it:
Create a new file in your VS Code project folder called `README.md`, paste this text inside, and save it. VS Code will automatically format it into a beautiful document.
