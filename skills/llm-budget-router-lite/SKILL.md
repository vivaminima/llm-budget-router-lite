---
name: llm-budget-router-lite
description: Shows what each LLM call cost from a local JSONL cost ledger, by task, provider and model. Use when the user asks, for example, "what did my LLM calls cost", "show the cost ledger", "budget this month", "이번 달 API 비용 보여 줘".
---

# llm-budget-router-lite

Shows what each LLM call cost from a local JSONL cost ledger, by task, provider and model. A small Python tool shipped in this plugin: standard library only, nothing to install, runs locally.

## Run

Use `python` (Windows: `py -3` also works; macOS/Linux: `python3`). On Windows add `-X utf8` if the console shows broken characters.

    python "${CLAUDE_PLUGIN_ROOT}/route.py" --ledger
    python "${CLAUDE_PLUGIN_ROOT}/route.py" --ledger --ledger-file <costs.jsonl> --month YYYY-MM

Run it from the user's project folder and pass the path they mean. If no path is given, ask which folder or file to check.

## Read the result

Routing a new call (--task) sends a request to a paid API with the user's own key; only do that when the user asks for it explicitly.

Show the tool's own output (shortened if long) and then a two- or three-line summary. Do not change the user's files unless they ask.
