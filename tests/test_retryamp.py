import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from retryamp.core import parse_spec, analyze, Call, worst_severity
from retryamp.cli import main

HERE = os.path.dirname(__file__)
EX = os.path.join(HERE, "..", "examples")


def codes(report):
    return sorted({f["code"] for f in report["findings"]})


def spec(calls, services=None, **defaults):
    return parse_spec({"defaults": defaults, "services": services or {}, "calls": calls})


class AmplificationTests(unittest.TestCase):
    def test_product_along_chain(self):
        s = spec([{"from": "a", "to": "b", "attempts": 3, "backoff_ms": 10, "jitter": True},
                  {"from": "b", "to": "c", "attempts": 4, "backoff_ms": 10, "jitter": True}],
                 {"a": {"entry": True}, "b": {}, "c": {}})
        r = analyze(s)
        self.assertEqual(r["amplification"]["a"]["c"], 12)
        self.assertIn("RA001", codes(r))
        self.assertIn("RA002", codes(r))

    def test_diamond_sums_paths(self):
        s = spec([{"from": "a", "to": "b", "attempts": 2, "backoff_ms": 1, "jitter": True},
                  {"from": "a", "to": "c"},
                  {"from": "b", "to": "d"},
                  {"from": "c", "to": "d"}],
                 {"a": {"entry": True, "rps": 10}, "b": {}, "c": {}, "d": {"capacity_rps": 25}})
        r = analyze(s)
        self.assertEqual(r["amplification"]["a"]["d"], 3)
        self.assertEqual(r["worst_case_rps"]["d"], 30)
        self.assertIn("RA009", codes(r))
        self.assertNotIn("RA002", codes(r))

    def test_fanout_counts(self):
        s = spec([{"from": "a", "to": "b", "fanout": 5}], {"a": {"entry": True}, "b": {}})
        self.assertEqual(analyze(s)["amplification"]["a"]["b"], 5)

    def test_entries_inferred_when_unmarked(self):
        s = spec([{"from": "a", "to": "b"}], {"a": {}, "b": {}})
        self.assertEqual(list(analyze(s)["amplification"]), ["a"])

    def test_max_amp_threshold(self):
        s = spec([{"from": "a", "to": "b", "attempts": 3, "backoff_ms": 5, "jitter": True}],
                 {"a": {"entry": True}, "b": {}}, max_amplification=2)
        self.assertIn("RA001", codes(analyze(s)))


class PolicyTests(unittest.TestCase):
    def test_non_idempotent_retry(self):
        s = spec([{"from": "a", "to": "b", "attempts": 2, "idempotent": False, "backoff_ms": 5, "jitter": True}])
        self.assertIn("RA005", codes(analyze(s)))

    def test_non_idempotent_single_try_ok(self):
        s = spec([{"from": "a", "to": "b", "idempotent": False}])
        self.assertEqual(codes(analyze(s)), [])

    def test_zero_backoff_and_no_jitter(self):
        r = analyze(spec([{"from": "a", "to": "b", "attempts": 2}]))
        sev = {f["severity"] for f in r["findings"] if f["code"] == "RA006"}
        self.assertEqual(sev, {"warn"})
        r = analyze(spec([{"from": "a", "to": "b", "attempts": 2, "backoff_ms": 10}]))
        sev = {f["severity"] for f in r["findings"] if f["code"] == "RA006"}
        self.assertEqual(sev, {"info"})

    def test_budget_exceeds_deadline(self):
        s = spec([{"from": "a", "to": "b", "attempts": 3, "per_try_timeout_ms": 400,
                   "backoff_ms": 100, "jitter": True}],
                 {"a": {"deadline_ms": 1000}, "b": {}})
        f = [x for x in analyze(s)["findings"] if x["code"] == "RA003"]
        self.assertEqual(len(f), 1)
        self.assertIn("only 2 of 3", f[0]["message"])

    def test_per_try_shorter_than_callee_deadline(self):
        s = spec([{"from": "a", "to": "b", "per_try_timeout_ms": 100}],
                 {"a": {}, "b": {"deadline_ms": 500}})
        f = [x for x in analyze(s)["findings"] if x["code"] == "RA004"]
        self.assertEqual(f[0]["severity"], "info")

    def test_missing_per_try_timeout(self):
        s = spec([{"from": "a", "to": "b", "attempts": 2, "backoff_ms": 5, "jitter": True}])
        self.assertIn("RA010", codes(analyze(s)))

    def test_cycle(self):
        s = spec([{"from": "a", "to": "b"}, {"from": "b", "to": "a"}], {"a": {}, "b": {}})
        r = analyze(s)
        self.assertEqual(codes(r), ["RA007"])

    def test_undeclared_service(self):
        s = spec([{"from": "a", "to": "ghost"}], {"a": {"entry": True}})
        self.assertIn("RA008", codes(analyze(s)))

    def test_bad_attempts(self):
        with self.assertRaises(ValueError):
            spec([{"from": "a", "to": "b", "attempts": 0}])

    def test_backoff_math(self):
        c = Call("a", "b", attempts=4, per_try_timeout_ms=100, backoff_ms=50,
                 backoff_multiplier=2, max_backoff_ms=150)
        # backoffs 50, 100, 150(capped from 200)
        self.assertEqual(c.backoff_total_ms(), 300)
        self.assertEqual(c.worst_case_ms(), 700)


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(list(args))
        return rc, buf.getvalue()

    def test_examples(self):
        rc, out = self.run_cli(os.path.join(EX, "checkout.toml"))
        self.assertEqual(rc, 1)
        self.assertIn("x36", out)
        rc, out = self.run_cli(os.path.join(EX, "fixed.toml"))
        self.assertEqual(rc, 0)
        self.assertIn("no findings", out)

    def test_json_and_fail_on(self):
        rc, out = self.run_cli(os.path.join(EX, "checkout.toml"), "--format", "json", "--fail-on", "never")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(worst_severity(data), "error")

    def test_json_spec_and_bad_ext(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "g.json")
            with open(p, "w") as fh:
                json.dump({"calls": [{"from": "a", "to": "b", "attempts": 2}]}, fh)
            rc, _ = self.run_cli(p, "--fail-on", "warn")
            self.assertEqual(rc, 1)
            bad = os.path.join(d, "g.yaml")
            open(bad, "w").close()
            self.assertEqual(main([bad]), 2)


if __name__ == "__main__":
    unittest.main()
