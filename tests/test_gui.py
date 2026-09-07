"""Tests for the knob GUI (tanglebrain/gui).

Hermetic: the view functions and the pure `dispatch` router are exercised directly — no socket is
bound and no network/subprocess runs (run_once is mocked). Covers secret-safety (key_ref is a ref
string, never resolved), the view shapes, run handling, and HTTP routing.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tanglebrain.gui import server, views
from tanglebrain.roster import Invoke, Roster, RosterEntry, packaged_roster_path
from tanglebrain.totals import default_totals_path
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
    def setUp(self):
        # Isolate the state root: these tests read the measurement store, and a suite that reads
        # (or migrates) the operator's real one is not hermetic and its results depend on the
        # machine it ran on.
        self.state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        env = patch.dict(os.environ, {"TANGLEBRAIN_STATE_DIR": self.state}, clear=False)
        env.start()
        self.addCleanup(env.stop)

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

    def test_stats_carries_measurement_health(self):
        # The panel is long-lived, so `record_task`'s once-per-process stderr notice is printed at
        # most once for the whole process and is effectively invisible here — this payload is the
        # only way a store that broke at hour six reaches the panel. (The panel does not poll; it
        # refetches on load, after a run, and after a pricing save.)
        with patch("tanglebrain.gui.views.read_records", return_value=[]):
            out = views.view_stats()
        self.assertEqual(out["health"], [])

    def test_stats_reports_a_damaged_store(self):
        # Exercised in the damaged state: a healthy run would pass against a payload that always
        # reported an empty list.
        # The path is computed from the code rather than assumed: TANGLEBRAIN_STATE_DIR *is* the
        # state root, with no product subdirectory under it.
        Path(default_totals_path()).write_text("{not json", encoding="utf-8")
        with patch("tanglebrain.gui.views.read_records", return_value=[]):
            out = views.view_stats()
        self.assertEqual(len(out["health"]), 1)
        self.assertIn("totals", out["health"][0])

    def test_the_panel_actually_renders_the_health_findings(self):
        # Pins the consumer, not just the producer. `view_stats` emitting `health` is covered
        # twice over, but deleting the render line — or renaming the payload key on either side —
        # left every test green while the panel silently stopped reporting that tasks are not
        # being recorded. That is the same shape as the untested `--stats` call site, one layer
        # out: the two ends of a contract were each pinned and the wire between them was not.
        panel = (Path(__file__).resolve().parents[1]
                 / "tanglebrain" / "gui" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("d.health", panel, "the panel must read the payload key view_stats writes")
        self.assertIn("⚠ measurement:", panel)
        self.assertIn("esc(finding)", panel, "server-composed findings must stay escaped")

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
        self.assertEqual(delegates["linkage_lost"], 1)
        self.assertEqual(delegates["by_backend"]["local-x"]["count"], 1)

    def test_includes_parent_task_tree(self):
        # The panel's delegate card renders the by_parent tree, so view_stats must carry it through.
        recs = [
            {"kind": "delegate", "model": "local-x", "parent_task_id": "p1"},
            {"kind": "delegate", "model": "local-x", "parent_task_id": "p2"},
            {"kind": "delegate", "model": "local-x"},
        ]
        with patch("tanglebrain.gui.views.read_records", return_value=recs):
            out = views.view_stats()
        by_parent = out["summary"]["delegates"]["by_parent"]
        self.assertEqual({k for k in by_parent if k != "unlinked"}, {"p1", "p2"})
        self.assertEqual(by_parent["unlinked"]["count"], 1)

    def test_stats_view_reads_stored_lifetime_totals_not_just_rows(self):
        # The panel's half of the same pin. `/api/stats` returns `rollup`'s dict verbatim, so the
        # panel is a second renderer of the figure; with only rows read it would silently show a
        # shrinking headline the moment rows are folded away.
        (Path(self.state) / "totals.json").write_text(
            json.dumps({"tasks": 11, "spend_avoided_usd": 4.25}), encoding="utf-8"
        )
        rows = [{"tier": "local", "in_tokens_est": 10, "out_tokens_est": 20,
                 "cloud_equiv_usd": 1.0, "spend_avoided_usd": 1.0}]
        with patch("tanglebrain.gui.views.read_records", return_value=rows):
            out = views.view_stats()
        self.assertEqual(out["summary"]["tasks"], 12)             # 11 stored + 1 row
        self.assertEqual(out["summary"]["spend_avoided_usd"], 5.25)


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
        self.assertEqual(run.call_args.kwargs["origin"], "gui")  # #74 attribution

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
    def setUp(self):
        # Isolate the state root: these tests read the measurement store, and a suite that reads
        # (or migrates) the operator's real one is not hermetic and its results depend on the
        # machine it ran on.
        self.state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        env = patch.dict(os.environ, {"TANGLEBRAIN_STATE_DIR": self.state}, clear=False)
        env.start()
        self.addCleanup(env.stop)

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

    def test_post_non_json_content_type_is_415_view_never_invoked(self):
        # A cross-origin browser fetch can POST text/plain to localhost with no CORS preflight —
        # /api/run spends real backend quota, so non-JSON must never reach a view (issue #72).
        # All three POST endpoints ride the same gate.
        body = json.dumps({"prompt": "hi"}).encode()
        for path in ("/api/run", "/api/pricing", "/api/roster"):
            with patch("tanglebrain.gui.views.run_once") as run, \
                 patch("tanglebrain.gui.views.save_pricing") as pricing, \
                 patch("tanglebrain.gui.views.save_roster_edits") as roster:
                status, ctype, out = server.dispatch(
                    "POST", path, body, content_type="text/plain;charset=UTF-8"
                )
            self.assertEqual(status, 415, path)
            self.assertIn("application/json", ctype)
            self.assertIn("application/json", json.loads(out)["error"])
            for view in (run, pricing, roster):
                view.assert_not_called()

    def test_post_charset_qualified_json_content_type_accepted(self):
        body = json.dumps({"prompt": "hi"}).encode()
        with patch("tanglebrain.gui.views.run_once", return_value=("ok", None)):
            status, _, out = server.dispatch(
                "POST", "/api/run", body, content_type="application/json; charset=utf-8"
            )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(out)["ok"])


class LiveHandlerTest(unittest.TestCase):
    """Loopback-socket tests proving the real Handler wiring for the #72 hardening."""

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_malformed_content_length_is_400_not_a_reset(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        connection.putrequest("POST", "/api/run")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "abc")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        self.assertIn("Content-Length", json.loads(response.read())["error"])

    def test_negative_content_length_clamps_to_empty_body(self):
        # max(0, …) must keep rfile.read(-1) from ever blocking on the socket; the request then
        # proceeds with an empty body and gets the view's own 400, not a hang or a reset.
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        connection.putrequest("POST", "/api/run")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "-5")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        self.assertIn("prompt is required", json.loads(response.read())["error"])

    def test_text_plain_post_is_415_over_the_wire(self):
        # Proves the Handler threads the real Content-Type header into dispatch.
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/run",
            data=json.dumps({"prompt": "hi"}).encode(),
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        with patch("tanglebrain.gui.views.run_once") as run:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 415)
        run.assert_not_called()


PANEL = Path(__file__).resolve().parents[1] / "tanglebrain" / "gui" / "static" / "index.html"

#: A URL that would leave this machine: any absolute or scheme-relative reference inside an
#: attribute, a `url(...)`, or a template literal — the panel is full of the last, so a
#: ``fetch(`https://…`)`` is the likeliest way one would arrive. Anchored on the opening
#: quote/backtick/paren so a bare `//` — a JS comment or a division, both of which the panel
#: contains — is not mistaken for one. Shared with the test that proves it fires, so the guard and
#: its proof cannot drift apart.
#:
#: **Not total, stated so nobody reads it as total:** an unquoted `<script src=https://…>` slips
#: it. Closing that means parsing HTML; the quoted and templated forms are the ones a person
#: actually writes.
OFF_MACHINE_URL = r"""["'(`]\s*(?:https?:)?//[^"')`\s]+"""


class PanelLayoutTest(unittest.TestCase):
    """The panel's shell: two views behind a sidebar, keyboard-reachable, nothing dropped.

    Asserted against the shipped source, which is the same technique
    `test_the_panel_actually_renders_the_health_findings` uses and for the same reason: there is
    no JS engine in this suite, and the alternative to reading the file is asserting nothing about
    the surface an operator actually looks at.

    The restructure these tests guard moved four cards between containers. The failure mode that
    matters is not a broken selector — it is a card quietly not arriving, which every other test
    in this module would sail past because they all exercise the *views*, never the page.
    """

    def setUp(self):
        self.panel = PANEL.read_text(encoding="utf-8")

    def test_both_views_ship(self):
        self.assertIn('id="view-chat"', self.panel)
        self.assertIn('id="view-settings"', self.panel)

    def test_every_registered_view_has_its_section_and_its_nav_link(self):
        # `VIEWS` maps a key to a section id, and the nav link id is derived as `nav-<key>`. The
        # three are joined by naming convention alone, and `showView()` dereferences all of them
        # unguarded — so a typo in any one throws inside the loop before the remaining views are
        # processed, which can leave a view permanently hidden and its cards at "loading…". The
        # registry is read out of the shipped source rather than restated here, so this cannot
        # pass by agreeing with a copy of the list.
        registry = re.search(r"const VIEWS = \{(.*?)\};", self.panel, re.DOTALL)
        self.assertIsNotNone(registry, "the panel must declare a VIEWS registry")
        pairs = re.findall(r"(\w+):\s*\"([^\"]+)\"", registry.group(1))
        self.assertTrue(pairs, "VIEWS must be a non-empty key -> section-id map")
        for key, section_id in pairs:
            with self.subTest(view=key):
                self.assertIn(f'id="{section_id}"', self.panel)
                self.assertIn(f'id="nav-{key}"', self.panel)
                self.assertIn(f'href="#/{key}"', self.panel)

    def test_the_sidebar_links_to_every_view(self):
        # The nav is the only way to reach a view, so a view without a link is unreachable.
        self.assertIn('id="nav-chat"', self.panel)
        self.assertIn('href="#/chat"', self.panel)
        self.assertIn('id="nav-settings"', self.panel)
        self.assertIn('href="#/settings"', self.panel)

    def test_navigation_uses_real_links(self):
        # Anchors, not click-handled divs: keyboard focus, Enter, browser back/forward and
        # "open in a new tab" all come free from the element and are lost the moment it stops
        # being one.
        for view in ("chat", "settings"):
            self.assertIn(f'<a class="navlink" id="nav-{view}" href="#/{view}">', self.panel)

    def test_chat_is_the_default_view_and_settings_starts_hidden(self):
        # An absent or unroutable `#/` hash must land somewhere rather than showing two views
        # or none.
        self.assertIn('const DEFAULT_VIEW = "chat";', self.panel)
        self.assertIn("showView(routedView() || DEFAULT_VIEW);", self.panel)
        settings = self.panel[self.panel.index('id="view-settings"') :]
        opening_tag = settings[: settings.index(">")]
        # The bare attribute, not a substring: `aria-hidden="true"` contains "hidden" and would
        # satisfy assertIn while hiding the view from assistive tech only, leaving it painted.
        self.assertRegex(opening_tag, r"(?:^|\s)hidden(?:\s|$)")

    def test_hidden_survives_a_later_display_rule(self):
        # Nothing sets `display` on a view or its ancestors today — `.app` is the flex container
        # and `.main`/`.wrap` carry none. The rule is defensive: whatever first gives a view or its
        # container a display rule would otherwise beat the bare `hidden` attribute and paint both
        # views at once. A sticky banner above the views is the near occasion (#162).
        self.assertIn(".view[hidden] { display: none; }", self.panel)

    def test_the_active_view_is_announced_not_just_painted(self):
        # A colour change alone tells a screen-reader user nothing about which view they are in.
        self.assertIn('setAttribute("aria-current", "page")', self.panel)
        self.assertIn('removeAttribute("aria-current")', self.panel)

    def test_showing_a_view_hides_every_other_one(self):
        # The load-bearing line, and the one every neighbouring assertion left unpinned: invert or
        # delete it and the initial markup still parses as "Settings hidden", the nav still paints
        # and announces correctly, and the panel shows both views at once after the first switch.
        # Whitespace-tolerant so a reformat does not force an assertion edit.
        self.assertRegex(self.panel, r"\$\(VIEWS\[key\]\)\.hidden\s*=\s*!active\s*;")

    def test_a_hash_change_switches_view(self):
        self.assertIn('window.addEventListener("hashchange"', self.panel)
        self.assertIn("if (view !== null) showView(view);", self.panel)

    def test_the_router_owns_only_its_own_prefix(self):
        """A fragment that is not a route must leave the view alone.

        The regression: the skip link this panel ships writes `#main`. A router that claims the
        whole fragment namespace reads that as an unroutable view and falls back to the default,
        so the keyboard user it exists for is thrown out of the view they were reading — and the
        address bar keeps `#main`, so a reload lands them there too. The same shape returns with
        #162's banner anchors and #164's roster modals, which is why the fix is a prefix the
        router owns rather than a special case for `#main`.

        Asserted against the source, like every other test in this class: there is no JS engine
        here, so this pins the construction that makes the bug unexpressible rather than
        executing the handler. Stated because the difference matters — it would not catch a
        second router added elsewhere on the page.
        """
        self.assertIn('const ROUTE_PREFIX = "#/";', self.panel)
        self.assertIn("if (!hash.startsWith(ROUTE_PREFIX)) return null;", self.panel)
        # Null and the default must stay distinguishable: collapsing them is the bug itself.
        self.assertIn("if (view !== null) showView(view);", self.panel)

    def test_the_skip_target_can_receive_focus(self):
        # Without tabindex the skip link scrolls but leaves focus behind in Safari, so the next
        # Tab resumes from the link rather than entering the content.
        self.assertIn('<main class="main" id="main" tabindex="-1">', self.panel)

    def test_the_skip_link_becomes_visible_when_focused(self):
        # The link is parked off-screen at left:-9999px and only this rule brings it back. Delete
        # it and the skip link is permanently invisible while every other assertion about it —
        # that it exists, that its target is focusable — still passes.
        self.assertIn(".skip:focus { left: 16px; }", self.panel)

    def test_focus_is_visible_on_the_nav(self):
        # An acceptance criterion of this chunk: the nav is the panel's first navigation surface
        # and a keyboard user must be able to see where they are in it.
        self.assertIn(".navlink:focus-visible {", self.panel)
        self.assertIn("outline: 2px solid var(--primary-bright)", self.panel)

    def test_the_sidebar_can_actually_stick(self):
        # `position: sticky` on a flex item stretched to its container's height does nothing, so
        # the nav would scroll away up the Settings view — the long one, and the one you most
        # want the nav from. The explicit height is what makes the sticky real.
        sidebar = self.panel[self.panel.index("  .sidebar {") :]
        sidebar = sidebar[: sidebar.index("}")]
        self.assertIn("position: sticky", sidebar)
        self.assertIn("height: 100vh", sidebar)
        self.assertNotIn("align-self: stretch", sidebar)

    def test_landmarks_and_a_skip_link_exist(self):
        self.assertIn('<nav class="sidebar" aria-label="Primary">', self.panel)
        self.assertIn('<main class="main" id="main"', self.panel)
        self.assertIn('<a class="skip" href="#main">', self.panel)

    def test_every_card_survived_the_restructure(self):
        # The regression this class exists for. Each of these is a distinct operator surface that
        # was on the single-column page before the split; losing one is silent everywhere else.
        for card in ("runResult", "statsCard", "rosterCard", "pricingCard"):
            with self.subTest(card=card):
                self.assertIn(f'id="{card}"', self.panel)

    def test_the_run_box_lives_in_the_chat_view(self):
        chat = self.panel[self.panel.index('id="view-chat"') : self.panel.index('id="view-settings"')]
        for control in ('id="prompt"', 'id="task"', 'id="local"', 'id="run"'):
            with self.subTest(control=control):
                self.assertIn(control, chat)

    def test_the_knobs_live_in_the_settings_view(self):
        settings = self.panel[self.panel.index('id="view-settings"') :]
        # Bounded at the section's end, like its chat-view twin: an unbounded slice runs to the
        # end of the file and would be satisfied by an id appearing anywhere below, including in
        # the script block.
        settings = settings[: settings.index("</section>")]
        for card in ('id="statsCard"', 'id="rosterCard"', 'id="pricingCard"'):
            with self.subTest(card=card):
                self.assertIn(card, settings)

    def test_the_panel_references_nothing_off_machine(self):
        """The binding constraint on this train: a default install reaches nothing off-machine.

        The panel is one packaged file served by a stdlib handler and must render with the machine
        offline. Nothing in the suite failed on an added `<script src="https://…">`, `@font-face`
        or `@import url(…)` before this test — and #176 proposes a Chart.js CDN tag, so the
        temptation is scheduled rather than hypothetical.
        """
        external = re.findall(OFF_MACHINE_URL, self.panel)
        self.assertEqual([], external, "the panel must reference no off-machine URL")
        for banned in ("@font-face", "@import"):
            with self.subTest(rule=banned):
                self.assertNotIn(banned, self.panel)

    def test_the_offline_guard_fires_on_the_shapes_it_claims(self):
        """The guard above is only worth having if it rejects what it says it rejects.

        A pattern asserted against a file that already conforms passes forever, including when it
        matches nothing at all. Running it over synthetic markup proves it without mutating the
        shipped panel — the same technique `test_gui_contrast.py` uses for its colour-literal
        guard, and for the same reason. The negative cases matter as much: `//` is also a JS
        comment and a division, and a guard that trips on those would be deleted within a week.
        """
        rejected = [
            '<script src="https://cdnjs.cloudflare.com/chart.min.js"></script>',
            "<script src='http://example.com/x.js'></script>",
            '<script src="//cdn.example.com/c.js"></script>',
            "@import url(https://fonts.googleapis.com/css?family=Inter);",
            "const r = await fetch(`https://api.example.com/v1/chart`);",
        ]
        allowed = [
            '<img src="/logo.png" alt="">',
            '<a href="#/chat">Chat</a>',
            "// a plain JS comment",
            "const ratio = a / b;  // not a URL",
        ]
        for markup in rejected:
            with self.subTest(rejected=markup):
                self.assertTrue(re.findall(OFF_MACHINE_URL, markup))
        for markup in allowed:
            with self.subTest(allowed=markup):
                self.assertEqual([], re.findall(OFF_MACHINE_URL, markup))

    def test_both_views_still_load_their_data_at_startup(self):
        # Splitting the page did not make any card's fetch conditional on its view being open.
        # `view_stats`'s docstring states the panel fetches on load; that stays true.
        self.assertIn("loadStats(); loadRoster(); loadPricing();", self.panel)


if __name__ == "__main__":
    unittest.main()
