#!/usr/bin/env python3
"""LLM Budget Router Lite - route LLM requests and see what each call costs.

  route.py --task NAME --brief FILE [--ctx FILE]... --out FILE     send one request, write the answer
  route.py --ledger [--month YYYY-MM]                              totals per provider and what is left

Which provider and model a task uses is set in router.toml (or .json). Prices live in
prices.toml, never in this file. API keys are read from environment variables only.

The cost ledger is JSON lines: time, task, provider, model, tokens, cost, output file.
It never holds a key, a request body or a response body. A failed call is recorded with
cost 0, and a retry is never counted as a second call.

Standard library only. No proxy, no server. Works on Windows, macOS and Linux.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

VERSION = "1.0.0"
TOOL = "LLM Budget Router Lite"
DEFAULT_MAX_TOKENS = 16000
STALE_PRICE_DAYS = 90
PLACEHOLDER = "REPLACE_"


class RouterError(Exception):
    """Bad configuration or usage. Exit code 2."""


class CallFailed(Exception):
    """One request attempt failed. Carries a short code only - never a response body."""

    def __init__(self, code, retryable):
        Exception.__init__(self, code)
        self.code, self.retryable = code, retryable


# --------------------------------------------------------------------------- small TOML reader

def _strip_comment(line):
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == "\\" and quote == '"':
                continue
            if ch == quote and not (quote == '"' and i and line[i - 1] == "\\"):
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return line[:i]
    return line


def _split_top(text, sep):
    """Split on sep outside quotes and brackets."""
    parts, depth, quote, start = [], 0, None, 0
    for i, ch in enumerate(text):
        if quote:
            if ch == quote and not (quote == '"' and text[i - 1] == "\\"):
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _toml_key(text, number):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if not text or not all(c.isalnum() or c in "-_" for c in text):
        raise RouterError("line %d: bad key %r" % (number, text))
    return text


def _toml_value(text, number):
    text = text.strip()
    if not text:
        raise RouterError("line %d: missing value" % number)
    if text[0] == '"' and text.endswith('"') and len(text) >= 2:
        try:
            return json.loads(text)
        except ValueError:
            raise RouterError("line %d: bad string" % number)
    if text[0] == "'" and text.endswith("'") and len(text) >= 2:
        return text[1:-1]
    if text[0] == "[" and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_toml_value(p, number) for p in _split_top(inner, ",") if p.strip()] if inner else []
    if text in ("true", "false"):
        return text == "true"
    try:
        return int(text.replace("_", ""))
    except ValueError:
        pass
    try:
        return float(text.replace("_", ""))
    except ValueError:
        pass
    if text[:4].isdigit() and text[4:5] == "-":            # a bare date stays a string
        return text
    raise RouterError("line %d: cannot read value %r (strings need quotes)" % (number, text))


def parse_toml(text):
    """Read the TOML subset this tool uses: [table] and [a."b c"] headers, key = value with
    strings, numbers, true/false, dates and arrays (an array may span lines). No inline tables."""
    root = {}
    table = root
    pending = None
    for number, raw in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        line = _strip_comment(raw).strip()
        if pending is not None:
            pending[1] += " " + line
            if pending[1].count("[") > pending[1].count("]"):
                continue
            table[pending[0]] = _toml_value(pending[1], number)
            pending = None
            continue
        if not line:
            continue
        if line.startswith("["):
            if not line.endswith("]") or line.startswith("[["):
                raise RouterError("line %d: bad table header %r" % (number, line))
            table = root
            for part in _split_top(line[1:-1], "."):
                table = table.setdefault(_toml_key(part, number), {})
                if not isinstance(table, dict):
                    raise RouterError("line %d: %r is already a value" % (number, part.strip()))
            continue
        halves = _split_top(line, "=")
        if len(halves) < 2:
            raise RouterError("line %d: expected key = value" % number)
        key, value = _toml_key(halves[0], number), "=".join(halves[1:]).strip()
        if value.startswith("[") and value.count("[") > value.count("]"):
            pending = [key, value]
            continue
        table[key] = _toml_value(value, number)
    if pending is not None:
        raise RouterError("array for %r is never closed" % pending[0])
    return root


def read_text(path):
    raw = Path(path).read_bytes()
    for bom, codec in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if raw.startswith(bom):
            return raw[len(bom):].decode(codec)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RouterError("%s is not UTF-8 text" % Path(path).name)


def load_table(path):
    """A .json file is read as JSON, anything else as TOML."""
    path = Path(path)
    if not path.is_file():
        raise RouterError("file not found: %s" % path)
    text = read_text(path)
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise RouterError("%s: invalid JSON (%s)" % (path.name, str(exc)[:60]))
        if not isinstance(data, dict):
            raise RouterError("%s: top level must be an object" % path.name)
        return data
    try:
        return parse_toml(text)
    except RouterError as exc:
        raise RouterError("%s: %s" % (path.name, exc))


# --------------------------------------------------------------------------- prices

def parse_day(value):
    try:
        return datetime.date(*[int(p) for p in str(value).split("-")])
    except (ValueError, TypeError):
        return None


def load_prices(path):
    """prices.toml -> {"as_of": date or None, "models": {model: (input_per_mtok, output_per_mtok)}}."""
    data = load_table(path)
    models = {}
    for name, row in (data.get("models") or {}).items():
        try:
            models[name] = (float(row["input_per_mtok"]), float(row["output_per_mtok"]))
        except (KeyError, TypeError, ValueError):
            raise RouterError("%s: model %r needs input_per_mtok and output_per_mtok" % (Path(path).name, name))
    return {"as_of": parse_day(data.get("as_of")), "as_of_text": str(data.get("as_of", "")), "models": models}


def cost_of(prices, model, input_tokens, output_tokens):
    """Cost in USD, or None when the model is not in the price table (recorded as "unknown", never 0)."""
    row = prices["models"].get(model)
    if row is None:
        return None
    return round((input_tokens * row[0] + output_tokens * row[1]) / 1000000.0, 6)


def price_warning(prices, today=None):
    """One warning line when the price table has no date or is more than 90 days old."""
    today = today or datetime.date.today()
    if prices["as_of"] is None:
        return "WARNING: prices.toml has no valid as_of date; check the prices on the providers' pricing pages."
    age = (today - prices["as_of"]).days
    if age > STALE_PRICE_DAYS:
        return ("WARNING: prices.toml is %d days old (as_of %s); check the prices on the providers' pricing pages."
                % (age, prices["as_of_text"]))
    return None


# --------------------------------------------------------------------------- providers

def _anthropic_request(model, prompt, max_tokens, key):
    return ("/v1/messages",
            {"x-api-key": key, "anthropic-version": "2026-10-21", "content-type": "application/json"},
            {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]})


def _anthropic_response(data):
    usage = data.get("usage") or {}
    text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
    stop = data.get("stop_reason")
    return {"text": text, "refused": stop == "refusal", "truncated": stop == "max_tokens",
            "input_tokens": int(usage.get("input_tokens", 0)) + int(usage.get("cache_creation_input_tokens", 0))
            + int(usage.get("cache_read_input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0))}


def _openai_request(model, prompt, max_tokens, key):
    return ("/v1/chat/completions",
            {"Authorization": "Bearer " + key, "content-type": "application/json"},
            {"model": model, "max_completion_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]})


def _openai_response(data):
    usage = data.get("usage") or {}
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return {"text": message.get("content") or "", "refused": bool(message.get("refusal")),
            "truncated": choice.get("finish_reason") == "length",
            "input_tokens": int(usage.get("prompt_tokens", 0)), "output_tokens": int(usage.get("completion_tokens", 0))}


PROVIDERS = {
    "anthropic": {"env": "ANTHROPIC_API_KEY", "base_url": "https://api.anthropic.com",
                  "request": _anthropic_request, "response": _anthropic_response},
    "openai": {"env": "OPENAI_API_KEY", "base_url": "https://api.openai.com",
               "request": _openai_request, "response": _openai_response},
}


def http_post_json(url, headers, body, timeout):
    """POST JSON, return the parsed JSON reply. Errors become CallFailed with a short code;
    the reply body of a failed request is never read into a message or a log."""
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CallFailed("http_%d" % exc.code, exc.code in (408, 409, 429) or exc.code >= 500)
    except (socket.timeout, TimeoutError):
        raise CallFailed("timeout", True)
    except urllib.error.URLError:
        raise CallFailed("network", True)
    except ValueError:
        raise CallFailed("bad_reply", False)


def call_provider(provider, model, prompt, max_tokens, env, post=http_post_json, base_url=None, timeout=600.0):
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise RouterError("unknown provider %r (known: %s)" % (provider, ", ".join(sorted(PROVIDERS))))
    key = (env.get(spec["env"]) or "").strip()
    if not key:
        raise RouterError("environment variable %s is not set" % spec["env"])
    path, headers, body = spec["request"](model, prompt, max_tokens, key)
    data = post((base_url or spec["base_url"]).rstrip("/") + path, headers, body, timeout)
    if not isinstance(data, dict):
        raise CallFailed("bad_reply", False)
    try:
        return spec["response"](data)
    except (AttributeError, TypeError, ValueError, IndexError):
        raise CallFailed("bad_reply", False)


# --------------------------------------------------------------------------- ledger

def now_local():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def append_ledger(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as handle:
        handle.write((json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def read_ledger(path):
    """Return (records, unreadable_line_count). A missing file is an empty ledger."""
    path = Path(path)
    if not path.is_file():
        return [], 0
    records, bad = [], 0
    for line in path.read_bytes().split(b"\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8"))
            if not isinstance(record, dict) or "call_id" not in record:
                raise ValueError
            records.append(record)
        except (ValueError, UnicodeDecodeError):
            bad += 1
    return records, bad


def summarize(records, month=None, since=None):
    """Totals per provider. A call is one call_id: its failed attempts cost 0 and are not calls.

    month "YYYY-MM" or since "YYYY-MM-DD" narrow the records by their time stamp."""
    calls = {}
    for r in records:
        at = str(r.get("at", ""))
        if month and at[:7] != month:
            continue
        if since and at[:10] < since:
            continue
        entry = calls.setdefault(r["call_id"], {"provider": r.get("provider", "?"), "done": None, "failed": 0})
        if r.get("status") in ("ok", "refused"):
            entry["done"] = r
            entry["provider"] = r.get("provider", "?")
        else:
            entry["failed"] += 1
    out = {}
    for entry in calls.values():
        row = out.setdefault(entry["provider"], {"calls": 0, "failed_calls": 0, "retries": 0, "input_tokens": 0,
                                                 "output_tokens": 0, "cost": 0.0, "unknown": 0})
        done = entry["done"]
        if done is None:
            row["failed_calls"] += 1
            continue
        row["calls"] += 1
        row["retries"] += entry["failed"]
        row["input_tokens"] += int(done.get("input_tokens", 0))
        row["output_tokens"] += int(done.get("output_tokens", 0))
        if isinstance(done.get("cost_usd"), (int, float)) and not isinstance(done.get("cost_usd"), bool):
            row["cost"] += float(done["cost_usd"])
        else:
            row["unknown"] += 1
    return out


def usd(amount):
    return "$%.2f" % amount if abs(amount) >= 1 else "$%.4f" % amount


def closing_line(summary):
    """The one closing line of the free edition - real numbers only, nothing when there are no calls."""
    calls = sum(r["calls"] for r in summary.values())
    if not calls:
        return None
    return ("This month: %s across %d call%s. Budget caps and expiry-first routing are in Pro."
            % (usd(sum(r["cost"] for r in summary.values())), calls, "" if calls == 1 else "s"))


def render_ledger(config, prices, ledger_path, month=None, today=None, closing=True, title=None):
    today = today or datetime.date.today()
    month = month or today.strftime("%Y-%m")
    records, bad = read_ledger(ledger_path)
    summary = summarize(records, month=month)
    lines = ["%s - cost ledger: %s" % (title or "%s %s" % (TOOL, VERSION), Path(ledger_path).name), "Month %s" % month, "",
             "%-12s %6s %7s %12s %12s %12s %8s" % ("provider", "calls", "failed", "input tok", "output tok",
                                                    "cost USD", "unknown")]
    for provider in sorted(summary):
        r = summary[provider]
        lines.append("%-12s %6d %7d %12d %12d %12s %8d" % (provider, r["calls"], r["failed_calls"],
                                                           r["input_tokens"], r["output_tokens"],
                                                           "%.4f" % r["cost"], r["unknown"]))
    if not summary:
        lines.append("(no calls recorded this month)")
    unknown = sum(r["unknown"] for r in summary.values())
    if unknown:
        lines += ["", "%d call%s with unknown price: add the model to prices.toml. Unknown is not counted as $0 "
                      "- the totals above leave it out." % (unknown, "" if unknown == 1 else "s")]
    balances = config.get("balances") or {}
    if balances:
        lines += ["", "Balance (what you entered in [balances], minus recorded cost):"]
        for provider in sorted(balances):
            entry = balances[provider]
            amount = entry.get("amount") if isinstance(entry, dict) else entry
            since = str(entry.get("since", "")) if isinstance(entry, dict) else ""
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                raise RouterError("[balances] %s: amount must be a number" % provider)
            used = summarize(records, since=since or None).get(provider, {"cost": 0.0, "unknown": 0})
            lines.append("  %-12s %s - %s = %s%s%s" % (
                provider, usd(amount), usd(used["cost"]), usd(amount - used["cost"]),
                " (since %s)" % since if since else "",
                " (+%d call(s) with unknown price not subtracted)" % used["unknown"] if used["unknown"] else ""))
    if bad:
        lines += ["", "WARNING: %d unreadable line(s) in the ledger were skipped." % bad]
    warning = price_warning(prices, today)
    lines += ["", warning or "Prices as of %s (%d days old)." % (prices["as_of_text"], (today - prices["as_of"]).days)]
    line = closing_line(summary) if closing else None
    if line:
        lines += ["", line]
    return "\n".join(lines)


# --------------------------------------------------------------------------- one routed call

def build_prompt(brief_path, ctx_paths):
    parts = [read_text(brief_path).strip()]
    for path in ctx_paths:
        parts.append("--- context: %s ---\n%s" % (Path(path).name, read_text(path).strip()))
    return "\n\n".join(parts) + "\n"


def task_route(config, task):
    """(provider, model, max_tokens) for a task name."""
    tasks = config.get("tasks") or {}
    entry = tasks.get(task)
    if not isinstance(entry, dict):
        raise RouterError("task %r is not in the config (known: %s)" % (task, ", ".join(sorted(tasks)) or "none"))
    provider, model = entry.get("provider"), entry.get("model")
    if not provider or not model:
        raise RouterError("task %r needs provider and model" % task)
    if str(model).startswith(PLACEHOLDER):
        raise RouterError("task %r: put a real model id in the config instead of %r" % (task, model))
    return str(provider), str(model), int(entry.get("max_tokens", DEFAULT_MAX_TOKENS))


def run_call(task, provider, model, prompt, out_path, ledger_path, prices, env, max_tokens=DEFAULT_MAX_TOKENS,
             retries=2, post=http_post_json, sleep=time.sleep, base_url=None, extra=None, force=False):
    """Send one request (with retries), write the answer, append to the ledger. Returns the last record."""
    out_path = Path(out_path)
    if out_path.exists() and not force:
        raise RouterError("output file already exists: %s (use --force to replace it)" % out_path)
    call_id = uuid.uuid4().hex
    base = {"call_id": call_id, "task": task, "provider": provider, "model": model, "out": str(out_path)}
    base.update(extra or {})
    attempt = 0
    while True:
        attempt += 1
        try:
            reply = call_provider(provider, model, prompt, max_tokens, env, post=post, base_url=base_url)
        except CallFailed as exc:
            record = dict(base, at=now_local(), attempt=attempt, status="failed", error=exc.code,
                          input_tokens=0, output_tokens=0, cost_usd=0.0)
            append_ledger(ledger_path, record)
            if exc.retryable and attempt <= retries:
                sleep(min(30.0, 2.0 ** attempt))
                continue
            return record
        cost = cost_of(prices, model, reply["input_tokens"], reply["output_tokens"])
        record = dict(base, at=now_local(), attempt=attempt, status="refused" if reply["refused"] else "ok",
                      input_tokens=reply["input_tokens"], output_tokens=reply["output_tokens"],
                      cost_usd="unknown" if cost is None else cost)
        if reply["truncated"]:
            record["truncated"] = True
        if not reply["refused"]:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(reply["text"])
        append_ledger(ledger_path, record)
        return record


def describe(record):
    cost = record["cost_usd"]
    head = "%s %s/%s" % (record["status"].upper(), record["provider"], record["model"])
    if record["status"] == "failed":
        return "%s after %d attempt(s): %s. Recorded with cost 0." % (head, record["attempt"], record["error"])
    tail = "in=%d out=%d cost=%s" % (record["input_tokens"], record["output_tokens"],
                                     "unknown (model not in prices.toml)" if cost == "unknown" else usd(cost))
    if record["status"] == "refused":
        return "%s %s - the provider declined the request; no output file was written." % (head, tail)
    return "%s %s -> %s%s" % (head, tail, record["out"],
                              " (answer was cut off at max_tokens)" if record.get("truncated") else "")


# --------------------------------------------------------------------------- cli

def safe_stdio():
    """Never crash on a console that cannot show a character (cp949, ascii): replace it instead."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and (stream.encoding or "").lower().replace("-", "").replace("_", "") != "utf8":
                stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def find_file(explicit, name, config_path=None):
    """An explicit path wins; otherwise next to the config, the current folder, then next to this script."""
    if explicit:
        return Path(explicit)
    places = ([Path(config_path).resolve().parent] if config_path else []) + [Path.cwd(), Path(__file__).resolve().parent]
    for folder in places:
        if (folder / name).is_file():
            return folder / name
    raise RouterError("%s not found (looked next to the config, in the current folder and next to route.py)" % name)


def ledger_path_of(config, config_path, explicit=None):
    if explicit:
        return Path(explicit)
    name = (config.get("ledger") or {}).get("path", "costs.jsonl")
    path = Path(str(name))
    return path if path.is_absolute() else Path(config_path).resolve().parent / path


def build_parser(prog="route", version=None):
    parser = argparse.ArgumentParser(prog=prog, description="Route one LLM request and record what it cost.")
    parser.add_argument("--task", help="task name from the config")
    parser.add_argument("--brief", help="file with the request text")
    parser.add_argument("--ctx", action="append", default=[], help="extra context file (repeatable)")
    parser.add_argument("--out", help="file to write the answer to")
    parser.add_argument("--force", action="store_true", help="allow --out to replace an existing file")
    parser.add_argument("--config", help="router.toml or router.json (default: ./router.toml)")
    parser.add_argument("--prices", help="prices.toml (default: next to the config)")
    parser.add_argument("--ledger-file", help="cost ledger file (default: [ledger] path in the config)")
    parser.add_argument("--retries", type=int, default=2, help="retries after a rate limit, server or network error")
    parser.add_argument("--ledger", action="store_true", help="show totals per provider and what is left")
    parser.add_argument("--month", help="with --ledger: YYYY-MM (default: this month)")
    parser.add_argument("--version", action="version", version=version or "%s %s" % (TOOL, VERSION))
    return parser


def main(argv=None, env=None, post=http_post_json, sleep=time.sleep):
    safe_stdio()
    args = build_parser().parse_args(argv)
    env = os.environ if env is None else env
    try:
        config_path = find_file(args.config, "router.toml")
        config = load_table(config_path)
        prices = load_prices(find_file(args.prices, "prices.toml", config_path))
        ledger = ledger_path_of(config, config_path, args.ledger_file)
        if args.ledger:
            if args.month and (len(args.month) != 7 or parse_day(args.month + "-01") is None):
                raise RouterError("--month must look like 2026-11")
            print(render_ledger(config, prices, ledger, month=args.month))
            return 0
        if not (args.task and args.brief and args.out):
            raise RouterError("--task, --brief and --out are required (or use --ledger)")
        provider, model, max_tokens = task_route(config, args.task)
        base_url = ((config.get("providers") or {}).get(provider) or {}).get("base_url")
        prompt = build_prompt(args.brief, args.ctx)
        warning = price_warning(prices)
        if warning:
            print(warning, file=sys.stderr)
        record = run_call(args.task, provider, model, prompt, args.out, ledger, prices, env, max_tokens=max_tokens,
                          retries=max(0, args.retries), post=post, sleep=sleep, base_url=base_url, force=args.force)
        print(describe(record))
        return 0 if record["status"] == "ok" else 1
    except RouterError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("ERROR: %s" % type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
