"""Tests for the knob GUI (tanglebrain/gui).

Hermetic: the view functions and the pure `dispatch` router are exercised directly — no socket is
bound and no network/subprocess runs (run_once is mocked). Covers secret-safety (key_ref is a ref
string, never resolved), the view shapes, run handling, and HTTP routing.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from tanglebrain.gui import server, views
from tanglebrain.roster import Invoke, Roster, RosterEntry, packaged_roster_path
from tanglebrain.router import RouterError


def _entry(eid, tier, *, key_ref=None, model=None, kind="cli", good_at=(), orch=False):
    return RosterEntry(
        id=eid, tier=tier,
        invoke=Invoke(kind=kind, model=model, key_ref=key_ref, cmd=["x"] if kind == "cli" else None),
        cost="free" if tier == "local" else "subscription",
        good_at=list(good_at), can_orchestrate=orch,
    )


class ViewRosterTest(unittest.TestCase):
    def test_packaged_roster_shape(self):
        # Pin to the packaged example (env override) so this is independent of any operator roster
        # at ~/.config/tanglebrain/roster.yaml on the dev machine. R2a: the packaged default ships
        # one active entry — the free local tier (the opt-in sub/paid tiers are commented examples).
        with patch.dict(os.environ, {"TANGLEBRAIN_ROSTER": str(packaged_roster_path())}, clear=False):
            out = views.view_roster()
        ids = {e["id"] for e in out["entries"]}
        self.assertEqual(ids, {"local-ollama"})
        local = next(e for e in out["entries"] if e["id"] == "local-ollama")
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

    def test_surfaces_enabled_and_budget(self):
        # The panel shows the per-key kill-switch + the display-only monthly budget.
        paid = RosterEntry(
            id="gpt5", tier="api",
            invoke=Invoke(kind="api", base_url="u", model="gpt-5", key_ref="file:/k.key"),
            enabled=False, budget_usd_month=25.0,
        )
        with patch("tanglebrain.gui.views.load_roster", return_value=Roster([paid])):
            e = views.view_roster()["entries"][0]
        self.assertFalse(e["enabled"])
        self.assertEqual(e["budget_usd_month"], 25.0)

    def test_default_entry_enabled_true_no_budget(self):
        with patch("tanglebrain.gui.views.load_roster",
                   return_value=Roster([_entry("claude", "sub")])):
            e = views.view_roster()["entries"][0]
        self.assertTrue(e["enabled"])
        self.assertIsNone(e["budget_usd_month"])


class ViewSettingsTest(unittest.TestCase):
    def test_packaged_gate_is_off(self):
        # The shipped settings.yaml keeps paid billing off — the panel must report that.
        self.assertFalse(views.view_settings()["api_billing_enabled"])

    def test_reports_gate_on_when_enabled(self):
        from tanglebrain.settings import Settings
        with patch("tanglebrain.gui.views.load_settings", return_value=Settings(api_billing_enabled=True)):
            self.assertTrue(views.view_settings()["api_billing_enabled"])


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

    def test_includes_delegate_breakdown(self):
        recs = [
            {"kind": "task", "tier": "sub", "in_tokens_est": 5, "out_tokens_est": 5,
             "cloud_equiv_usd": 0.5, "spend_avoided_usd": 0.5},
            {"kind": "delegate", "model": "local-x", "in_tokens_est": 40, "out_tokens_est": 60,
             "cloud_equiv_usd": 2.0},
        ]
        with patch("tanglebrain.gui.views.read_records", return_value=recs):
            out = views.view_stats()
        # Headline stays task-only; delegates surface separately for the panel's fan-out breakdown.
        self.assertEqual(out["summary"]["tasks"], 1)
        delegates = out["summary"]["delegates"]
        self.assertEqual(delegates["count"], 1)
        self.assertEqual(delegates["by_backend"]["local-x"]["count"], 1)


class RunPromptTest(unittest.TestCase):
    def test_happy_path_reports_served(self):
        served = {"path": "router", "tier": "sub", "model": "claude"}
        with patch("tanglebrain.gui.views.run_once", return_value=("hello back", served)) as run:
            out = views.run_prompt({"prompt": "hi", "task": "code"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["text"], "hello back")
        self.assertEqual(out["served"]["model"], "claude")
        self.assertEqual(run.call_args.kwargs["task"], "code")
        self.assertTrue(run.call_args.kwargs["return_served"])  # uses the returned meta, no log re-read

    def test_does_not_reread_log(self):
        # The race fix: run_prompt must NOT call read_records (served comes from run_once).
        with patch("tanglebrain.gui.views.run_once", return_value=("x", None)), \
             patch("tanglebrain.gui.views.read_records", side_effect=AssertionError("must not re-read log")):
            out = views.run_prompt({"prompt": "hi"})
        self.assertIsNone(out["served"])

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
        with patch("tanglebrain.gui.views.run_once", return_value=("x", None)) as run:
            views.run_prompt({"prompt": "hi", "local": True})
        self.assertTrue(run.call_args.kwargs["local"])


class SavePricingViewTest(unittest.TestCase):
    def _payload(self, **over):
        base = {"reference_model": "Test Model", "input_per_mtok": 2.0,
                "output_per_mtok": 8.0, "placeholder": False}
        base.update(over)
        return base

    def test_valid_save_persists_and_returns_view(self):
        with patch("tanglebrain.gui.views.save_pricing") as save, \
             patch("tanglebrain.gui.views.view_pricing", return_value={"reference_model": "Test Model"}):
            out = views.save_pricing_view(self._payload())
        self.assertTrue(out["ok"])
        self.assertEqual(out["pricing"]["reference_model"], "Test Model")
        save.assert_called_once()

    def test_invalid_does_not_save(self):
        with patch("tanglebrain.gui.views.save_pricing") as save:
            out = views.save_pricing_view(self._payload(input_per_mtok=-1))
        self.assertFalse(out["ok"])
        self.assertIn("input_per_mtok", out["error"])
        save.assert_not_called()


class SaveRosterViewTest(unittest.TestCase):
    def test_happy_path_returns_updated_roster(self):
        with patch("tanglebrain.gui.views.save_roster_edits") as save, \
             patch("tanglebrain.gui.views.load_roster",
                   return_value=Roster([_entry("claude", "sub")])):
            out = views.save_roster_view({"id": "claude", "fields": {"enabled": False}})
        save.assert_called_once_with("claude", {"enabled": False})
        self.assertTrue(out["ok"])
        self.assertIn("entries", out["roster"])

    def test_missing_id_or_fields_rejected(self):
        for bad in ({"fields": {"enabled": False}}, {"id": "claude"}, {"id": "claude", "fields": {}}):
            self.assertFalse(views.save_roster_view(bad)["ok"])

    def test_edit_error_returned_not_raised(self):
        from tanglebrain.roster_edit import RosterEditError
        with patch("tanglebrain.gui.views.save_roster_edits", side_effect=RosterEditError("nope")):
            out = views.save_roster_view({"id": "x", "fields": {"enabled": True}})
        self.assertFalse(out["ok"])
        self.assertIn("nope", out["error"])


class DispatchTest(unittest.TestCase):
    def test_get_index_is_html(self):
        status, ctype, body = server.dispatch("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"TangleBrain", body)

    def test_get_logo_is_png(self):
        status, ctype, body = server.dispatch("GET", "/logo.png")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/png")
        self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))  # PNG magic
        self.assertGreater(len(body), 0)

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

    def test_get_settings_json(self):
        from tanglebrain.settings import Settings
        with patch("tanglebrain.gui.views.load_settings", return_value=Settings(api_billing_enabled=True)):
            status, ctype, body = server.dispatch("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        self.assertTrue(json.loads(body)["api_billing_enabled"])

    def test_unknown_path_404(self):
        status, _, body = server.dispatch("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("not found", json.loads(body)["error"])

    def test_post_run_valid(self):
        body = json.dumps({"prompt": "hi"}).encode()
        with patch("tanglebrain.gui.views.run_once", return_value=("ok", None)):
            status, _, out = server.dispatch("POST", "/api/run", body)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(out)["ok"])

    def test_post_roster_valid(self):
        body = json.dumps({"id": "claude", "fields": {"enabled": False}}).encode()
        with patch("tanglebrain.gui.views.save_roster_edits"), \
             patch("tanglebrain.gui.views.load_roster",
                   return_value=Roster([_entry("claude", "sub")])):
            status, _, out = server.dispatch("POST", "/api/roster", body)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(out)["ok"])

    def test_post_roster_bad_request_400(self):
        body = json.dumps({"id": "claude"}).encode()  # no fields
        status, _, out = server.dispatch("POST", "/api/roster", body)
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(out)["ok"])

    def test_post_pricing_valid(self):
        body = json.dumps({"reference_model": "M", "input_per_mtok": 1.0,
                           "output_per_mtok": 2.0, "placeholder": False}).encode()
        with patch("tanglebrain.gui.views.save_pricing"), \
             patch("tanglebrain.gui.views.view_pricing", return_value={"reference_model": "M"}):
            status, _, out = server.dispatch("POST", "/api/pricing", body)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(out)["ok"])

    def test_post_pricing_invalid_400(self):
        body = json.dumps({"reference_model": "", "input_per_mtok": 1.0, "output_per_mtok": 2.0}).encode()
        with patch("tanglebrain.gui.views.save_pricing") as save:
            status, _, out = server.dispatch("POST", "/api/pricing", body)
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(out)["ok"])
        save.assert_not_called()

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
