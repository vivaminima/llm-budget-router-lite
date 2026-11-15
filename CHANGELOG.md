# Changelog

## 1.0.0
First release.

- `route.py --task NAME --brief FILE [--ctx FILE]... --out FILE`: one request to the provider and model the task is
  mapped to in `router.toml` (or `.json`). Providers: Anthropic and OpenAI. Keys come from environment variables only.
- Cost ledger in JSON lines: time, task, provider, model, tokens, cost, output file. No key, no request text, no
  response text. A failed call is recorded with cost 0; a retried call is counted once.
- Prices live in `prices.toml`, not in the code. A model that is not listed is recorded as `unknown`, never 0.
  A price table more than 90 days old gives a warning.
- `--ledger`: totals per provider for the month and the balance left from what you entered.
- 28 tests, all offline (provider replies are mocked).
