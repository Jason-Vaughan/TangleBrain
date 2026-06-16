"""Tests for the C5a knob GUI (tanglebrain/gui).

Hermetic: the view functions and the pure `dispatch` router are exercised directly — no socket is
bound and no network/subprocess runs (run_once is mocked). Covers secret-safety (key_ref is a ref
string, never resolved), the view shapes, run handling, and HTTP routing.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tanglebrain.gui import server, views
from tanglebrain.roster import Invoke, Roster, RosterEntry
from tanglebrain.router import RouterError


def _entry(eid, tier, *, key_ref=None, model=None, kind="cli", good_at=(), orch=False):
    return RosterEntry(
        id=eid, tier=tier,
        invoke=Invoke(kind=kind, model=model, key_ref=key_ref, cmd=["x"] if kind == "cli" else None),
        cost="free" if tier == "local" else "flat-rate",
        good_at=list(good_at), can_orchestrate=orch,
    )


class ViewRosterTest(unittest.TestCase):
    def test_packaged_roster_shape(self):
        # Reads the real packaged roster.yaml (hermetic file read, no network).
        out = views.view_roster()
        ids = {e["id"] for e in out["entries"]}
        self.assertIn("gpt-oss-120b", ids)
        self.assertIn("claude", ids)
        local = next(e for e in out["entries"] if e["id"] == "gpt-oss-120b")
        self.assertEqual(local["tier"], "local")
        self.assertIn("kind", local["invoke"])

    def test_key_ref_passed_through_not_resolved(self):
        # The secret-safety guarantee: key_ref is emitted verbatim as the reference string, and
        # no file is ever opened to resolve it.
        roster = Roster([_entry("local", "local", kind="openai-compat", model="m",
                                 key_ref="file:/secret/path.key")])
        with patch("tanglebrain.gui.views.load_roster", return_value=roster), \
             patch("builtins.open", side_effect=AssertionError("must not read key file")):
            out = views.view_roster()
        self.assertEqual(out["entries"][0]["invoke"]["key_ref"], "file:/secret/path.key")

    def test_no_secret_fields_leak(self):
        # Only the documented invoke subset is exposed (no cmd/scrub_env/delegate_args).
        roster = Roster([_entry("claude", "sub", key_ref="env:ANTHROPIC", good_at=["reasoning"], orch=True)])
        with patch("tanglebrain.gui.views.load_roster", return_value=roster):
            inv = views.view_roster()["entries"][0]["invoke"]
        self.assertEqual(set(inv), {"kind", "base_url", "model", "parse", "key_ref"})


class ViewPricingTest(unittest.TestCase):
    def test_packaged_pricing(self):
        out = views.view_pricing()
        self.assertFalse(out["is_placeholder"])
        self.assertEqual(out["input_per_mtok"], 3.0)
        self.assertEqual(out["output_per_mtok"], 15.0)


class ViewStatsTest(unittest.TestCase):
    def test_rolls_up_records(self):
        recs = [
            {"tier": "local", "in_tokens_est": 10, "out_tokens_est": 20,
             "cloud_equiv_usd": 1.0, "spend_avoided_usd": 1.0},
            {"tier": "sub", "in_tokens_est": 5, "out_tokens_est": 5,
             "cloud_equiv_usd": 0.5, "spend_avoided_usd": 0.5},
        ]
        with patch("tanglebrain.gui.views.read_records", return_value=recs):
            out = views.view_stats()
        self.assertEqual(out["summary"]["tasks"], 2)
        self.assertEqual(out["summary"]["by_tier"], {"local": 1, "sub": 1})
        self.assertAlmostEqual(out["summary"]["spend_avoided_usd"], 1.5)
        self.assertIn("is_placeholder", out)


class RunPromptTest(unittest.TestCase):
    def test_happy_path_reports_served(self):
        served = [{"path": "router", "tier": "sub", "model": "claude"}]
        with patch("tanglebrain.gui.views.run_once", return_value="hello back") as run, \
             patch("tanglebrain.gui.views.read_records", return_value=served):
            out = views.run_prompt({"prompt": "hi", "task": "code"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["text"], "hello back")
        self.assertEqual(out["served"]["model"], "claude")
        self.assertEqual(run.call_args.kwargs["task"], "code")

    def test_empty_prompt_rejected(self):
        out = views.run_prompt({"prompt": "   "})
        self.assertFalse(out["ok"])
        self.assertIn("required", out["error"])

    def test_missing_prompt_key_rejected(self):
        self.assertFalse(views.run_prompt({})["ok"])

    def test_backend_error_returned(self):
        with patch("tanglebrain.gui.views.run_once", side_effect=RouterError("all subs failed")):
            out = views.run_prompt({"prompt": "hi"})
        self.assertFalse(out["ok"])
        self.assertIn("all subs failed", out["error"])

    def test_local_flag_threaded(self):
        with patch("tanglebrain.gui.views.run_once", return_value="x") as run, \
             patch("tanglebrain.gui.views.read_records", return_value=[]):
            out = views.run_prompt({"prompt": "hi", "local": True})
        self.assertTrue(run.call_args.kwargs["local"])
        self.assertIsNone(out["served"])  # no records -> served is None


class DispatchTest(unittest.TestCase):
    def test_get_index_is_html(self):
        status, ctype, body = server.dispatch("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"TangleBrain", body)

    def test_get_roster_json(self):
        with patch("tanglebrain.gui.views.load_roster",
                   return_value=Roster([_entry("local", "local", kind="openai-compat", model="m")])):
            status, ctype, body = server.dispatch("GET", "/api/roster")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        self.assertEqual(json.loads(body)["entries"][0]["id"], "local")

    def test_get_stats_ignores_query_string(self):
        with patch("tanglebrain.gui.views.read_records", return_value=[]):
            status, _, _ = server.dispatch("GET", "/api/stats?t=123")
        self.assertEqual(status, 200)

    def test_unknown_path_404(self):
        status, _, body = server.dispatch("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("not found", json.loads(body)["error"])

    def test_post_run_valid(self):
        body = json.dumps({"prompt": "hi"}).encode()
        with patch("tanglebrain.gui.views.run_once", return_value="ok"), \
             patch("tanglebrain.gui.views.read_records", return_value=[]):
            status, _, out = server.dispatch("POST", "/api/run", body)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(out)["ok"])

    def test_post_run_bad_json_400(self):
        status, _, out = server.dispatch("POST", "/api/run", b"{not json")
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(out)["ok"])

    def test_post_run_non_object_400(self):
        status, _, _ = server.dispatch("POST", "/api/run", b"[1,2,3]")
        self.assertEqual(status, 400)

    def test_post_unknown_path_404(self):
        status, _, _ = server.dispatch("POST", "/api/nope", b"{}")
        self.assertEqual(status, 404)

    def test_empty_prompt_run_is_400(self):
        body = json.dumps({"prompt": ""}).encode()
        status, _, _ = server.dispatch("POST", "/api/run", body)
        self.assertEqual(status, 400)

    def test_unsupported_method_405(self):
        status, _, _ = server.dispatch("DELETE", "/api/roster")
        self.assertEqual(status, 405)

    def test_read_view_error_is_clean_json_500(self):
        # A failing read view returns a JSON 500, not a traceback to the client.
        from tanglebrain.roster import RosterError

        with patch("tanglebrain.gui.views.load_roster", side_effect=RosterError("bad roster yaml")):
            status, ctype, body = server.dispatch("GET", "/api/roster")
        self.assertEqual(status, 500)
        self.assertIn("application/json", ctype)
        self.assertIn("bad roster yaml", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
