# Route LLM requests and see what each call costs.

*Part of the [Vivaminima tools](https://github.com/vivaminima) for Claude Code users.*

<img src="https://raw.githubusercontent.com/vivaminima/vivaminima/main/img/run/llm-budget-router-lite.png" width="760" alt="llm-budget-router-lite: real output of the free version on a made-up sample">
Unlike LiteLLM or OpenRouter this is not a proxy or a server: it is one local Python file with no dependencies that sends one request, writes the answer to a file and keeps a cost ledger with no keys and no request or response text in it.

## New to this? Start here

**Who it's for:** you call the Anthropic or OpenAI API from your own scripts and want to see what each call cost this month, without a proxy, a server or another account. If you only use chat apps, you don't need this.

**What you need:** an API key from Anthropic or OpenAI (a developer account, separate from a chat subscription) and Python 3.8 or newer.

1. Download this repository (green **Code** button → **Download ZIP**) and unzip it anywhere.
2. In a terminal, set your key for this session only. Type it yourself; do not paste a key into a chat window.
   - Windows (PowerShell): `$env:ANTHROPIC_API_KEY = "..."`
   - macOS / Linux: `export ANTHROPIC_API_KEY="..."`
3. Open `prices.toml` and type in today's prices from the provider's official pricing page. Set `as_of` to today.
4. Write what you want in a text file called `brief.md`, then run:
   `python route.py --task draft --brief brief.md --out answer.md` and then `python route.py --ledger`

What you get back: the answer in `answer.md`, and a table of calls, tokens and cost for the month. The ledger never stores your key or the text of your request.

**LLM Budget Router Lite 1.0.0** - Anthropic and OpenAI, one command per request, one JSON-lines ledger.
If you need a gateway, load balancing or a hundred providers, use one of the tools above; this one does not try to
compete with them.

```
python route.py --task draft --brief brief.md --ctx notes.md --out answer.md
python route.py --ledger
```

```
OK anthropic/claude-opus-5-5 in=1000 out=500 cost=$0.0140 -> answer.md

LLM Budget Router Lite 1.0.0 - cost ledger: costs.jsonl
Month 2026-11

provider      calls  failed    input tok   output tok     cost USD  unknown
anthropic         2       0         2000         1000       0.0280        0
openai            1       0         2000         1000       0.0040        0

Balance (what you entered in [balances], minus recorded cost):
  anthropic    $25.00 - $0.0280 = $24.97
  openai       $10.00 - $0.0040 = $10.00

Prices as of 2026-11-03 (9 days old).

This month: $0.0320 across 3 calls. Budget caps and expiry-first routing are in Pro.
```

(The numbers above come from the package's own test, with mocked replies.)

## Set up

1. Python 3.8 or newer. Nothing to install.
2. Put your keys in environment variables: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`. The tool reads them from the
   environment only. It never writes a key anywhere.
3. Edit `router.toml`: one `[tasks.NAME]` table per task, with `provider` (`anthropic` or `openai`), `model`
   and optionally `max_tokens`. A `.json` file with the same shape works too (`--config router.json`).
4. Edit `prices.toml`: the models you use, in USD per million tokens. **Check the prices on the provider's
   official pricing page** and set `as_of` to the day you checked.

## What it records

One JSON line per attempt in the ledger (`costs.jsonl` by default):

```json
{"at": "2026-11-12T10:15:02+09:00", "attempt": 1, "call_id": "6f0c...", "cost_usd": 0.014, "input_tokens": 1000,
 "model": "claude-opus-5-5", "out": "answer.md", "output_tokens": 500, "provider": "anthropic", "status": "ok",
 "task": "draft"}
```

| Situation | What is recorded |
|---|---|
| The call works | `status: ok`, tokens as the provider reported them, cost from `prices.toml` |
| The call fails (bad request, rate limit, timeout, no network) | `status: failed`, cost `0`, a short code such as `http_429` - never the reply text |
| A failed call is retried and then works | The failed attempts and the good one share one `call_id`: it is counted as **one** call |
| The model is not in `prices.toml` | cost `"unknown"` - not `0`. `--ledger` says how many such calls there are and leaves them out of the totals |
| The provider declines the request | `status: refused`, tokens and cost recorded, no output file |
| The answer is cut off at `max_tokens` | `status: ok` with `truncated: true`, and a note on the console |

The ledger never holds an API key, the text you sent or the text that came back. The answer goes only to `--out`.

## Options

```
--task NAME            task name from the config
--brief FILE           the request text
--ctx FILE             extra context, added after the brief (repeatable)
--out FILE             where the answer goes; an existing file is not replaced unless --force
--config FILE          router.toml or router.json (default: ./router.toml)
--prices FILE          prices.toml (default: next to the config)
--ledger-file FILE     another ledger file
--retries N            retries after a rate limit, server or network error (default 2)
--ledger               totals per provider for the month, and the balance
--month YYYY-MM        with --ledger: another month
```

Exit codes: `0` done, `1` the call failed or was declined (recorded in the ledger), `2` bad config or usage
(nothing was sent).

## Limits - read these

- **Cost is an estimate from your price table.** The provider's invoice is the truth. Cached, batch and
  long-prompt prices are not modelled: every input token is priced at `input_per_mtok`.
- A timeout is recorded as failed with cost 0, but the provider may still have billed the request.
- The tool warns when `prices.toml` is more than 90 days old. It does not fetch prices.
- `config` files use a small TOML subset: tables, strings, numbers, true/false, dates and arrays. No inline
  tables, no `[[arrays of tables]]`.
- One request, one answer. No streaming, no tools, no conversation state.
- The provider calls in this release were tested against mocked replies only, not against the live services.
  The request shapes are the public Messages API (Anthropic) and Chat Completions API (OpenAI) as of the
  release date; if a provider changes them, set `[providers.NAME] base_url` or update `route.py`.

## Tested

Windows 11, Python 3.10.11: 28 tests, offline, including a cp949 console run. Other versions and platforms
have not been tested. Run them yourself with `python -m unittest discover -s tests`.

MIT licence. See CHANGELOG.md.

## More from Vivaminima

- **LLM Budget Router Pro ($19)**, the paid edition of this tool, one-time payment on Gumroad: https://vivastory4.gumroad.com/l/llm-budget-router-pro

  <a href="https://vivastory4.gumroad.com/l/llm-budget-router-pro"><img src="https://raw.githubusercontent.com/vivaminima/vivaminima/main/img/llm-budget-router-lite.png" width="480" alt="LLM Budget Router Pro"></a>

- **Token Audit Kit ($19)** — see how many tokens your CLAUDE.md and its @imports cost before your first prompt, and a step-by-step guide to split it. Free script: https://github.com/vivaminima/context-file-audit · Paid kit (store): https://vivastory4.gumroad.com
- Free tools: [skill-audit-kit](https://github.com/vivaminima/skill-audit-kit) · [secret-map](https://github.com/vivaminima/secret-map) · [inbox-ledger](https://github.com/vivaminima/inbox-ledger) · [release-gate](https://github.com/vivaminima/release-gate) · [skill-install-gate](https://github.com/vivaminima/skill-install-gate) · [console-safe](https://github.com/vivaminima/console-safe) · [checklist-stop](https://github.com/vivaminima/checklist-stop) · [handoff-check](https://github.com/vivaminima/handoff-check) · [llm-budget-router-lite](https://github.com/vivaminima/llm-budget-router-lite) · [go-no-go-verdict-skill](https://github.com/vivaminima/go-no-go-verdict-skill) · [ai-company-starter-kit](https://github.com/vivaminima/ai-company-starter-kit) · [beginner-readme-section](https://github.com/vivaminima/beginner-readme-section)

Vivaminima is not affiliated with, endorsed by or sponsored by Anthropic.
