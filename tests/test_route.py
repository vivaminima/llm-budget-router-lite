"""Tests for LLM Budget Router Lite. Offline: every provider call is a mock.
Run:  python -m unittest discover -s tests"""
import datetime
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import route

# Not a real key and not shaped like one: built at run time from harmless pieces.
FAKE_KEY = "-".join(["fake", "value", "for", "tests"])
BRIEF = "Summarise the quarterly numbers for the board."
ANSWER = "Revenue rose and costs fell."

CONFIG = """
[ledger]
path = "costs.jsonl"

[tasks.draft]
provider = "anthropic"
model = "model-a"
max_tokens = 500

[tasks.summarize]
provider = "openai"
model = "model-o"

[tasks.mystery]
provider = "openai"
model = "model-not-priced"

[tasks.todo]
provider = "openai"
model = "REPLACE_WITH_AN_OPENAI_MODEL_ID"

[balances.anthropic]
amount = 25.00

[balances.openai]
amount = 10.00
since = "2000-01-01"
"""

PRICES = """
as_of = "%s"
[models."model-a"]
input_per_mtok = 4.00
output_per_mtok = 20.00
[models."model-o"]
input_per_mtok = 1.00
output_per_mtok = 2.00
"""


def anthropic_reply(tin=1000, tout=500, text=ANSWER, stop="end_turn"):
    return {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}],
            "stop_reason": stop, "usage": {"input_tokens": tin, "output_tokens": tout}}


def openai_reply(tin=2000, tout=1000, text=ANSWER, finish="stop"):
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": tin, "completion_tokens": tout}}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.write_prices(datetime.date.today().isoformat())
        (self.root / "router.toml").write_text(CONFIG, encoding="utf-8")
        (self.root / "brief.md").write_text(BRIEF, encoding="utf-8")
        self.ledger = self.root / "costs.jsonl"
        self.env = {"ANTHROPIC" + "_API_KEY": FAKE_KEY, "OPENAI" + "_API_KEY": FAKE_KEY}
        self.sent = []

    def tearDown(self):
        self._tmp.cleanup()

    def write_prices(self, as_of):
        (self.root / "prices.toml").write_text(PRICES % as_of, encoding="utf-8")

    def post(self, *replies):
        """A fake transport that returns the given replies (or raises them) in order."""
        queue = list(replies)

        def fake(url, headers, body, timeout):
            self.sent.append({"url": url, "headers": headers, "body": body})
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return fake

    def run_main(self, *args, post=None):
        out, err = io.StringIO(), io.StringIO()
        argv = [str(a) for a in args] + ["--config", str(self.root / "router.toml")]
        with redirect_stdout(out), redirect_stderr(err):
            code = route.main(argv, env=self.env, post=post or self.post(), sleep=lambda s: None)
        return code, out.getvalue(), err.getvalue()

    def call(self, task="draft", post=None, out="answer.md", *extra):
        return self.run_main("--task", task, "--brief", self.root / "brief.md", "--out", self.root / out,
                             *extra, post=post)

    def records(self):
        return [json.loads(l) for l in self.ledger.read_text(encoding="utf-8").split("\n") if l.strip()]


class Routing(Base):
    def test_task_goes_to_its_provider_and_model(self):
        code, out, _ = self.call("draft", self.post(anthropic_reply()))
        self.assertEqual(code, 0, out)
        sent = self.sent[0]
        self.assertEqual(sent["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(sent["body"]["model"], "model-a")
        self.assertEqual(sent["body"]["max_tokens"], 500)
        self.assertEqual(sent["headers"]["x-api-key"], FAKE_KEY)
        self.assertNotIn("temperature", sent["body"])
        self.assertEqual((self.root / "answer.md").read_text(encoding="utf-8"), ANSWER)

    def test_openai_task_uses_the_openai_shape(self):
        code, out, _ = self.call("summarize", self.post(openai_reply()))
        self.assertEqual(code, 0, out)
        sent = self.sent[0]
        self.assertEqual(sent["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(sent["headers"]["Authorization"], "Bearer " + FAKE_KEY)
        self.assertEqual(sent["body"]["max_completion_tokens"], route.DEFAULT_MAX_TOKENS)

    def test_context_files_are_added_after_the_brief(self):
        (self.root / "notes one.md").write_text("Context A", encoding="utf-8")
        (self.root / "b.txt").write_text("Context B", encoding="utf-8-sig")
        self.call("draft", self.post(anthropic_reply()), "answer.md", "--ctx", self.root / "notes one.md",
                  "--ctx", self.root / "b.txt")
        prompt = self.sent[0]["body"]["messages"][0]["content"]
        self.assertLess(prompt.index(BRIEF), prompt.index("Context A"))
        self.assertLess(prompt.index("Context A"), prompt.index("Context B"))
        self.assertIn("--- context: notes one.md ---", prompt)

    def test_unknown_task_placeholder_model_and_missing_key_stop_before_any_request(self):
        for task, word in (("nope", "not in the config"), ("todo", "real model id")):
            code, out, err = self.call(task)
            self.assertEqual(code, 2)
            self.assertIn(word, err)
        self.env = {}
        code, out, err = self.call("draft")
        self.assertEqual(code, 2)
        self.assertIn("is not set", err)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.ledger.exists())

    def test_existing_output_is_not_replaced_without_force(self):
        (self.root / "answer.md").write_text("keep me", encoding="utf-8")
        code, out, err = self.call("draft", self.post(anthropic_reply()))
        self.assertEqual(code, 2)
        self.assertEqual((self.root / "answer.md").read_text(encoding="utf-8"), "keep me")
        self.assertEqual(self.sent, [])                                    # refused before spending anything
        code, out, err = self.call("draft", self.post(anthropic_reply()), "answer.md", "--force")
        self.assertEqual(code, 0)

    def test_json_config_works_too(self):
        cfg = {"tasks": {"draft": {"provider": "anthropic", "model": "model-a"}}}
        (self.root / "router.json").write_text(json.dumps(cfg), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = route.main(["--task", "draft", "--brief", str(self.root / "brief.md"), "--out",
                               str(self.root / "j.md"), "--config", str(self.root / "router.json")],
                              env=self.env, post=self.post(anthropic_reply()), sleep=lambda s: None)
        self.assertEqual(code, 0, err.getvalue())


class LedgerRecords(Base):
    def test_record_holds_time_task_model_tokens_cost_and_output_path(self):
        self.call("draft", self.post(anthropic_reply(1000, 500)))
        (r,) = self.records()
        self.assertEqual((r["task"], r["provider"], r["model"], r["status"]), ("draft", "anthropic", "model-a", "ok"))
        self.assertEqual((r["input_tokens"], r["output_tokens"]), (1000, 500))
        self.assertAlmostEqual(r["cost_usd"], (1000 * 4.0 + 500 * 20.0) / 1e6)      # 0.014
        self.assertTrue(r["out"].endswith("answer.md"))
        self.assertRegex(r["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        self.assertEqual(sorted(r), sorted(["at", "attempt", "call_id", "cost_usd", "input_tokens", "model", "out",
                                            "output_tokens", "provider", "status", "task"]))

    def test_no_key_request_text_or_answer_text_anywhere_but_the_output_file(self):
        code, out, err = self.call("draft", self.post(anthropic_reply()))
        code2, out2, err2 = self.run_main("--ledger")
        everything = self.ledger.read_text(encoding="utf-8") + out + err + out2 + err2
        for secret in (FAKE_KEY, BRIEF, ANSWER, "quarterly", "Revenue"):
            self.assertNotIn(secret, everything)

    def test_failed_call_is_recorded_with_cost_zero(self):
        code, out, _ = self.call("draft", self.post(route.CallFailed("http_400", False)))
        self.assertEqual(code, 1)
        (r,) = self.records()
        self.assertEqual((r["status"], r["cost_usd"], r["error"], r["attempt"]), ("failed", 0.0, "http_400", 1))
        self.assertFalse((self.root / "answer.md").exists())
        self.assertIn("cost 0", out)

    def test_retry_is_one_call_not_two(self):
        post = self.post(route.CallFailed("http_429", True), route.CallFailed("timeout", True), anthropic_reply(1000, 500))
        code, out, _ = self.call("draft", post)
        self.assertEqual(code, 0, out)
        records = self.records()
        self.assertEqual([r["status"] for r in records], ["failed", "failed", "ok"])
        self.assertEqual(len({r["call_id"] for r in records}), 1)
        self.assertEqual([r["cost_usd"] for r in records[:2]], [0.0, 0.0])
        row = route.summarize(records)["anthropic"]
        self.assertEqual((row["calls"], row["failed_calls"], row["retries"]), (1, 0, 2))
        self.assertAlmostEqual(row["cost"], 0.014)

    def test_retries_stop_and_the_call_counts_as_failed_once(self):
        post = self.post(*[route.CallFailed("http_503", True)] * 3)
        code, out, _ = self.call("draft", post)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.records()), 3)
        row = route.summarize(self.records())["anthropic"]
        self.assertEqual((row["calls"], row["failed_calls"], row["cost"]), (0, 1, 0.0))

    def test_refusal_is_recorded_and_writes_no_output(self):
        code, out, _ = self.call("draft", self.post(anthropic_reply(text="", stop="refusal")))
        self.assertEqual(code, 1)
        self.assertEqual(self.records()[0]["status"], "refused")
        self.assertFalse((self.root / "answer.md").exists())

    def test_cut_off_answer_is_flagged(self):
        code, out, _ = self.call("summarize", self.post(openai_reply(finish="length")))
        self.assertEqual(code, 0)
        self.assertTrue(self.records()[0]["truncated"])
        self.assertIn("cut off", out)

    def test_http_errors_never_carry_the_reply_body(self):
        import urllib.error

        def boom(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"body with " + FAKE_KEY.encode()))
        real = route.urllib.request.urlopen
        route.urllib.request.urlopen = boom
        try:
            with self.assertRaises(route.CallFailed) as caught:
                route.http_post_json("https://example.invalid/x", {}, {}, 1)
        finally:
            route.urllib.request.urlopen = real
        self.assertEqual((caught.exception.code, caught.exception.retryable), ("http_401", False))
        self.assertNotIn(FAKE_KEY, str(caught.exception))


class Prices(Base):
    def test_model_missing_from_the_price_table_is_unknown_not_zero(self):
        code, out, _ = self.call("mystery", self.post(openai_reply()))
        self.assertEqual(code, 0)
        self.assertEqual(self.records()[0]["cost_usd"], "unknown")
        self.assertIn("unknown", out)
        self.call("summarize", self.post(openai_reply(2000, 1000)), "b.md")
        code, text, _ = self.run_main("--ledger")
        self.assertIn("1 call with unknown price", text)
        row = route.summarize(self.records())["openai"]
        self.assertEqual((row["calls"], row["unknown"]), (2, 1))
        self.assertAlmostEqual(row["cost"], 0.004)                        # only the priced call

    def test_price_table_older_than_90_days_warns(self):
        old = (datetime.date.today() - datetime.timedelta(days=91)).isoformat()
        self.write_prices(old)
        code, out, err = self.call("draft", self.post(anthropic_reply()))
        self.assertIn("91 days old", err)
        code, text, _ = self.run_main("--ledger")
        self.assertIn("WARNING: prices.toml is 91 days old", text)

    def test_price_table_of_exactly_90_days_does_not_warn(self):
        self.write_prices((datetime.date.today() - datetime.timedelta(days=90)).isoformat())
        code, out, err = self.call("draft", self.post(anthropic_reply()))
        self.assertEqual(err, "")
        self.assertIn("(90 days old)", self.run_main("--ledger")[1])

    def test_missing_date_warns(self):
        (self.root / "prices.toml").write_text('[models."model-a"]\ninput_per_mtok = 1\noutput_per_mtok = 1\n',
                                               encoding="utf-8")
        self.assertIn("no valid as_of date", self.run_main("--ledger")[1])

    def test_shipped_price_file_has_a_date_and_no_prices_in_the_code(self):
        prices = route.load_prices(HERE / "prices.toml")
        self.assertIsNotNone(prices["as_of"])
        self.assertIn("claude-opus-5-5", prices["models"])
        source = (HERE / "route.py").read_text(encoding="utf-8")
        self.assertNotIn("per_mtok =", source)
        self.assertNotIn("claude-", source)                                # no model, so no price, lives in the code


class LedgerView(Base):
    def test_totals_balance_and_closing_line_use_the_real_numbers(self):
        self.call("draft", self.post(anthropic_reply(1000, 500)), "a.md")          # 0.014
        self.call("draft", self.post(anthropic_reply(1000, 500)), "b.md")          # 0.014
        self.call("summarize", self.post(openai_reply(2000, 1000)), "c.md")        # 0.004
        code, text, _ = self.run_main("--ledger")
        self.assertEqual(code, 0)
        self.assertRegex(text, r"anthropic\s+2\s+0\s+2000\s+1000\s+0\.0280")
        self.assertRegex(text, r"openai\s+1\s+0\s+2000\s+1000\s+0\.0040")
        self.assertIn("anthropic    $25.00 - $0.0280 = $24.97", text)
        self.assertIn("(since 2000-01-01)", text)
        self.assertTrue(text.rstrip().endswith(
            "This month: $0.0320 across 3 calls. Budget caps and expiry-first routing are in Pro."))

    def test_no_closing_line_when_nothing_was_called(self):
        code, text, _ = self.run_main("--ledger")
        self.assertEqual(code, 0)
        self.assertIn("(no calls recorded this month)", text)
        self.assertNotIn("Pro", text)

    def test_other_months_are_left_out(self):
        route.append_ledger(self.ledger, {"call_id": "x", "at": "2020-01-15T10:00:00+00:00", "provider": "openai",
                                          "status": "ok", "input_tokens": 1, "output_tokens": 1, "cost_usd": 5.0})
        code, text, _ = self.run_main("--ledger")
        self.assertIn("(no calls recorded this month)", text)
        self.assertIn("openai       $10.00 - $5.00 = $5.00", text)          # the balance still counts it
        code, text, _ = self.run_main("--ledger", "--month", "2020-01")
        self.assertRegex(text, r"openai\s+1\s+0")
        self.assertEqual(self.run_main("--ledger", "--month", "2020")[0], 2)

    def test_damaged_ledger_line_is_skipped_and_reported(self):
        self.call("draft", self.post(anthropic_reply()))
        with open(self.ledger, "ab") as handle:
            handle.write(b"{half a line\n")
        code, text, _ = self.run_main("--ledger")
        self.assertEqual(code, 0)
        self.assertIn("1 unreadable line(s)", text)


class Toml(unittest.TestCase):
    def test_subset(self):
        data = route.parse_toml('a = 1 # c\nb = "x # not a comment"\n[t."m-1.5"]\nlist = ["p:q", \'r\',\n  "s"]\n'
                                "day = 2026-12-31\nok = true\nf = 1.5\n[t.u]\nk = 'v'\n")
        self.assertEqual(data, {"a": 1, "b": "x # not a comment",
                                "t": {"m-1.5": {"list": ["p:q", "r", "s"], "day": "2026-12-31", "ok": True, "f": 1.5},
                                      "u": {"k": "v"}}})

    def test_errors_name_the_line(self):
        for bad in ("a = hello", "[[x]]", "just words", "a = [1, 2"):
            with self.assertRaises(route.RouterError):
                route.parse_toml("ok = 1\n" + bad + "\n")


class Console(Base):
    def test_cp949_and_ascii_consoles_exit_cleanly(self):
        route.append_ledger(self.ledger, {"call_id": "x", "at": route.now_local(), "provider": "한글공급자",
                                          "status": "ok", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.5})
        for enc in ("cp949", "ascii"):
            env = dict(os.environ, PYTHONIOENCODING=enc, PYTHONUTF8="0")
            r = subprocess.run([sys.executable, str(HERE / "route.py"), "--ledger", "--config",
                                str(self.root / "router.toml")], capture_output=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(b"across 1 call.", r.stdout)

    def test_messages_are_ascii_and_the_code_is_standard_library_only(self):
        import ast
        source = (HERE / "route.py").read_text(encoding="utf-8")
        self.assertTrue(all(ord(c) < 128 for c in source))
        allowed = {"__future__", "argparse", "datetime", "json", "os", "socket", "sys", "time", "urllib", "uuid", "pathlib"}
        for node in ast.walk(ast.parse(source)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module] if isinstance(node, ast.ImportFrom) else []
            for name in names:
                self.assertIn(name.split(".")[0], allowed)

    def test_readme_first_two_lines(self):
        lines = (HERE / "README.md").read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[0], "# Route LLM requests and see what each call costs.")
        self.assertIn("LiteLLM", lines[1])
        self.assertIn("OpenRouter", lines[1])
        text = "\n".join(lines).lower()
        for word in ("re" + "fund", "money" + " back", "$" + "19", "buy now"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
