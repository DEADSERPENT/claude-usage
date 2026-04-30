# Claude Usage Dashboard

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![claude-code](https://img.shields.io/badge/claude--code-black?style=flat-square)](https://claude.ai/code)

🚀 Track, analyze, and optimize your Claude usage — locally and in real-time.

---

## ✨ Features

• Real-time usage tracking (tokens, models, sessions)  
• Cost estimation based on API pricing  
• Local-first (no data leaves your machine)  
• CLI + Web Dashboard  
• Budget protection (alerts + circuit breaker)  
• Zero dependencies — runs with Python only  

---

## 📸 Preview

![Dashboard](docs/screenshot.png)

---

## ⚡ Quick Start

```bash
git clone https://github.com/DEADSERPENT/claude-usage
cd claude-usage

# Scan usage logs
python cli.py scan

# Launch dashboard
python cli.py dashboard
````

---

## 🧠 Why This Matters

• Avoid unexpected API costs
• Understand token usage patterns
• Optimize prompts and model usage
• Get visibility beyond Claude’s UI

---

## 🛠️ Commands

```bash
python cli.py scan        # Load usage data
python cli.py today       # Today's stats
python cli.py stats       # All-time stats
python cli.py dashboard   # Open web UI
python cli.py api         # Start REST API
```

---

## 👤 Who It's For

• Developers using Claude Code
• Teams managing LLM costs
• AI builders & experimenters

---

## 🤝 Contributing

PRs, issues, and ideas are welcome!

---

## 📄 License

MIT License
