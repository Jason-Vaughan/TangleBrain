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
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tanglebrain.gui import server, views
from tanglebrain.measurement import rollup
from tanglebrain.roster import Invoke, Roster, RosterEntry, packaged_roster_path
from tanglebrain.totals import default_totals_path, empty_totals
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
        # Escaping used to be the call site's job (`esc(finding)`), which put the obligation on
        # every future caller. It now happens once inside the status bar's item builder, so a
        # caller cannot forget it. Same property, enforced structurally instead of by convention.
        self.assertIn("esc(text)", panel, "server-composed findings must stay escaped")
        self.assertIn("esc(extraClass)", panel, "both interpolations escape, or neither is safe")

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
        self.assertEqual(delegates["by_backend"], [{"id": "local-x", "count": 1}])

    def test_reports_how_many_parents_the_sub_calls_link_to(self):
        # The panel renders one number from the parent tree — how many top-level tasks the
        # sub-calls link back to — so the projection sends that number and not the tree. The tree
        # holds one key per parent task id, which makes it the largest unbounded structure the old
        # passthrough could put on the wire.
        recs = [
            {"kind": "delegate", "model": "local-x", "parent_task_id": "p1"},
            {"kind": "delegate", "model": "local-x", "parent_task_id": "p2"},
            {"kind": "delegate", "model": "local-x"},
        ]
        with patch("tanglebrain.gui.views.read_records", return_value=recs):
            out = views.view_stats()
        delegates = out["summary"]["delegates"]
        self.assertEqual(delegates["linked_parents"], 2)          # p1 and p2; "unlinked" is not one
        self.assertEqual(delegates["linkage_lost"], 1)
        self.assertNotIn("by_parent", delegates)

    def test_stats_view_reads_stored_lifetime_totals_not_just_rows(self):
        # The panel's half of the same pin. The panel is a second renderer of the headline figure;
        # with only rows read it would silently show a shrinking total the moment rows are folded
        # away.
        (Path(self.state) / "totals.json").write_text(
            json.dumps({"tasks": 11, "spend_avoided_usd": 4.25}), encoding="utf-8"
        )
        rows = [{"tier": "local", "in_tokens_est": 10, "out_tokens_est": 20,
                 "cloud_equiv_usd": 1.0, "spend_avoided_usd": 1.0}]
        with patch("tanglebrain.gui.views.read_records", return_value=rows):
            out = views.view_stats()
        self.assertEqual(out["summary"]["tasks"], 12)             # 11 stored + 1 row
        self.assertEqual(out["summary"]["spend_avoided_usd"], 5.25)


def _rollup_with_days(days, *, lifetime_spend=None, by_model=None, since=""):
    """Build a real rollup over stored totals carrying ``{day: spend}`` — no records, no files.

    Goes through `rollup` rather than hand-writing a summary dict so the projection is tested
    against the shape the product actually produces; a literal would drift the moment the rollup
    gains a field.
    """
    totals = empty_totals()
    for day, spend in days.items():
        totals["by_day"][day] = {
            "count": 1, "in_tokens_est": 0, "out_tokens_est": 0,
            "cloud_equiv_usd": spend, "spend_avoided_usd": spend,
        }
    for model, spend in (by_model or {}).items():
        totals["by_model"][model] = {
            "count": 1, "in_tokens_est": 0, "out_tokens_est": 0,
            "cloud_equiv_usd": spend, "spend_avoided_usd": spend,
        }
    totals["by_day_since"] = since
    totals["spend_avoided_usd"] = (
        sum(days.values()) if lifetime_spend is None else lifetime_spend
    )
    return rollup([], totals)


class StatsProjectionTest(unittest.TestCase):
    """The `/api/stats` payload contract — what the endpoint promises, not what the rollup holds."""

    def test_a_new_rollup_field_does_not_reach_the_panel(self):
        # The defect this projection exists to close (#223): the endpoint used to return the
        # rollup verbatim, so a field added for the CLI's benefit shipped to the browser with
        # nobody deciding it. Asserted on a field that does not exist rather than on the current
        # field list, because the failure mode is *addition*, and a list asserted against itself
        # would pass for every future leak.
        summary = _rollup_with_days({})
        summary["a_field_added_for_some_other_consumer"] = {"big": "x" * 1000}
        payload = views.project_stats_summary(summary)
        self.assertNotIn("a_field_added_for_some_other_consumer", payload)

    def test_the_contract_doc_lists_exactly_what_the_endpoint_sends(self):
        # The doc claims a *complete* field set and names what was removed, which is the shape
        # that decays within a merge unless something recomputes it: the other two tests here
        # guard additions to the ROLLUP and the presence of the rendered fields, and neither
        # notices a field added to the projection — the one edit that falsifies the sentence. It
        # matters more on this surface than on any other in the repo, because a ruled exception
        # lets this endpoint *remove* fields, so this list is the only statement a reader has of
        # what it does not send.
        doc = (Path(__file__).resolve().parents[1] / "docs" / "design" / "api-contract.md").read_text(
            encoding="utf-8"
        )
        section = doc[doc.index("The declared set, in full."):doc.index("What the reshaped")]
        declared_prose, removed_prose = section.split("Nothing else in")
        payload = views.project_stats_summary(_rollup_with_days({"2026-09-10": 1.0}))
        sent = set(payload) | set(payload["delegates"]) | {
            "summary", "pricing_ref", "is_placeholder", "health",
        }
        self.assertEqual(
            set(re.findall(r"`([a-z_]+)`", declared_prose)), sent,
            "docs/design/api-contract.md § Surface 4 names a field set that is no longer what "
            "project_stats_summary returns — update that section in the same commit",
        )
        # The removals are the other half of the claim, and they decay the same way: a field named
        # as dropped that the rollup no longer produces makes the sentence a historical note
        # dressed as a contract.
        rollup_keys = set(rollup([], empty_totals())) | set(rollup([], empty_totals())["delegates"])
        for name in re.findall(r"`([a-z_]+)`", removed_prose):
            with self.subTest(removed=name):
                self.assertIn(name, rollup_keys, "named as removed but the rollup does not hold it")
                self.assertNotIn(name, payload, "named as removed but still sent at the top level")
        self.assertNotIn("by_parent", payload["delegates"])

    def test_the_fields_the_panel_renders_all_survive(self):
        # The other direction: a projection that drops something the panel draws is a blank card.
        payload = views.project_stats_summary(
            _rollup_with_days({"2026-09-01": 1.0}, by_model={"local-a": 1.0})
        )
        for field in ("tasks", "spend_avoided_usd", "by_tier", "by_origin", "in_tokens_est",
                      "out_tokens_est", "by_model", "by_day", "by_day_since",
                      "spend_avoided_outside_days_usd", "delegates"):
            self.assertIn(field, payload)

    def test_the_day_series_is_capped_at_the_widest_window_the_panel_draws(self):
        days = {(date(2026, 1, 1) + timedelta(days=i)).isoformat(): 1.0 for i in range(120)}
        series = views.project_stats_summary(_rollup_with_days(days))["by_day"]
        self.assertEqual(len(series), views.STATS_DAY_WINDOW)
        # Newest-last, and it is the *newest* 90 that survive — a cap that kept the oldest would
        # draw a chart that never moves.
        self.assertEqual(series[-1]["day"], (date(2026, 1, 1) + timedelta(days=119)).isoformat())
        self.assertEqual(series[0]["day"], (date(2026, 1, 1) + timedelta(days=30)).isoformat())
        self.assertEqual([e["day"] for e in series], sorted(e["day"] for e in series))

    def test_an_idle_day_inside_the_covered_range_is_a_real_zero(self):
        series = views.project_stats_summary(
            _rollup_with_days({"2026-09-01": 2.0, "2026-09-04": 3.0})
        )["by_day"]
        self.assertEqual(
            [(e["day"], e["spend_avoided_usd"]) for e in series],
            [("2026-09-01", 2.0), ("2026-09-02", 0.0), ("2026-09-03", 0.0), ("2026-09-04", 3.0)],
        )

    def test_days_before_the_first_bucket_are_absent_not_zero(self):
        # The single most consequential rule here. Before the earliest surviving bucket, absence
        # means *unknown* — those days were either never recorded or evicted past retention — and
        # painting them as $0 asserts there was no activity on days whose activity was simply not
        # kept. The series must therefore START at the first covered day even when the window is
        # wider, and `by_day_since` (older than the first bucket once eviction bites) must not be
        # allowed to extend it.
        payload = views.project_stats_summary(
            _rollup_with_days({"2026-09-09": 1.0, "2026-09-10": 1.0}, since="2026-01-01")
        )
        self.assertEqual([e["day"] for e in payload["by_day"]], ["2026-09-09", "2026-09-10"])
        # The stamp still ships — the caption's wording needs it — it just is not the boundary.
        self.assertEqual(payload["by_day_since"], "2026-01-01")

    def test_no_buckets_yields_an_empty_series_rather_than_a_flat_line(self):
        payload = views.project_stats_summary(_rollup_with_days({}))
        self.assertEqual(payload["by_day"], [])
        self.assertEqual(payload["by_day_since"], "")

    def test_a_stray_far_past_key_cannot_inflate_the_payload(self):
        # A damaged or hand-edited store can hold a day key from any era. Filling forward from the
        # oldest key would materialize every day between — half a century of zeroes on a payload
        # meant to stay small. The window is counted back from the NEWEST bucket, which bounds the
        # work whatever the spread.
        days = {"1970-01-01": 5.0, "2026-09-10": 1.0}
        payload = views.project_stats_summary(_rollup_with_days(days))
        series = payload["by_day"]
        self.assertEqual(len(series), views.STATS_DAY_WINDOW)
        self.assertEqual(series[0]["day"], (date(2026, 9, 10) - timedelta(days=89)).isoformat())
        self.assertEqual(series[-1], {"day": "2026-09-10", "spend_avoided_usd": 1.0})
        # The 1970 dollars are bucketed, so they are not "uncharted" in this field's sense — they
        # are simply outside the window, which is the panel's own lifetime-vs-window comparison.
        self.assertEqual(payload["spend_avoided_outside_days_usd"], 0.0)
        self.assertLess(sum(e["spend_avoided_usd"] for e in series), payload["spend_avoided_usd"])

    def test_a_key_that_is_not_a_day_is_dropped_not_raised_on(self):
        # `normalize_totals` validates a bucket's *aggregate*, never its key, so a corrupt file
        # reaches here with whatever string it holds. "2026-13-45" is the interesting one: it
        # passes `is_day_key` (ten digits in the right places) and is not a date.
        #
        # "20260910" is the case that makes the format check load-bearing rather than belt and
        # braces: `date.fromisoformat` accepts several ISO spellings, so a key written in another
        # one parses to a date a real day key ALSO parses to — and the second one to be read would
        # silently replace the first. Only one spelling is this store's format.
        # Asserted in BOTH key orders, because which key a file happens to list first is arbitrary
        # — and that arbitrariness is the whole reason the format check has to be there. Read in
        # one order the impostor is overwritten and the defect hides; read in the other it wins and
        # the chart draws a figure the store never recorded.
        for keys in (
            {"banana": 1.0, "2026-13-45": 2.0, "20260910": 8.0, "2026-09-10": 4.0},
            {"banana": 1.0, "2026-13-45": 2.0, "2026-09-10": 4.0, "20260910": 8.0},
        ):
            with self.subTest(first=list(keys)[2]):
                payload = views.project_stats_summary(_rollup_with_days(keys))
                self.assertEqual([e["day"] for e in payload["by_day"]], ["2026-09-10"])
                self.assertEqual(payload["by_day"][0]["spend_avoided_usd"], 4.0)
                self.assertEqual(payload["spend_avoided_outside_days_usd"], 11.0)

    def test_spend_outside_the_day_buckets_is_zero_when_every_dollar_is_bucketed(self):
        # Exercised in both states deliberately: a caption that only ever runs against a store
        # with a gap would pass for an implementation that always claims one.
        payload = views.project_stats_summary(_rollup_with_days({"2026-09-10": 4.0}))
        self.assertEqual(payload["spend_avoided_outside_days_usd"], 0.0)

    def test_spend_recorded_before_per_day_tracking_shows_as_uncharted(self):
        # The install that upgraded yesterday: a lifetime figure built over months, one day of
        # per-day history. The chart cannot show the rest, and the caption says so from this field.
        payload = views.project_stats_summary(
            _rollup_with_days({"2026-09-10": 1.0}, lifetime_spend=9.5, since="2026-09-10")
        )
        self.assertEqual(payload["spend_avoided_outside_days_usd"], 8.5)

    def test_uncharted_spend_never_renders_as_a_negative(self):
        payload = views.project_stats_summary(
            _rollup_with_days({"2026-09-10": 4.0}, lifetime_spend=1.0)
        )
        self.assertEqual(payload["spend_avoided_outside_days_usd"], 0.0)

    def test_the_window_is_a_parameter_and_it_bounds_the_series(self):
        # `_day_series` takes the cap as an argument so the rule is one value rather than a literal
        # buried in a loop. Exercised at a width the default would hide: with 10 days recorded, a
        # 3-day window must yield the newest 3 and nothing older.
        days = {f"2026-09-{d:02d}": 1.0 for d in range(1, 11)}
        parsed = views._parse_day_buckets(_rollup_with_days(days)["by_day"])
        series = views._day_series(parsed, window=3)
        self.assertEqual([e["day"] for e in series], ["2026-09-08", "2026-09-09", "2026-09-10"])

    def test_by_model_is_ranked_biggest_saver_first_with_stable_ties(self):
        payload = views.project_stats_summary(
            _rollup_with_days({}, by_model={"zeta": 1.0, "alpha": 1.0, "big": 7.0})
        )
        self.assertEqual(
            payload["by_model"],
            [
                {"id": "big", "count": 1, "spend_avoided_usd": 7.0},
                {"id": "alpha", "count": 1, "spend_avoided_usd": 1.0},
                {"id": "zeta", "count": 1, "spend_avoided_usd": 1.0},
            ],
        )

    def test_the_model_breakdown_sums_to_the_headline_it_breaks_down(self):
        # A breakdown that does not add up to the figure above it is worse than none: a reader
        # checks one against the other and believes whichever they read second.
        payload = views.project_stats_summary(
            _rollup_with_days({}, by_model={"a": 1.25, "b": 2.5}, lifetime_spend=3.75)
        )
        self.assertAlmostEqual(
            sum(row["spend_avoided_usd"] for row in payload["by_model"]),
            payload["spend_avoided_usd"],
        )

    def test_the_delegate_backend_split_is_ranked_by_calls(self):
        recs = [
            {"kind": "delegate", "model": "busy"},
            {"kind": "delegate", "model": "busy"},
            {"kind": "delegate", "model": "quiet"},
        ]
        payload = views.project_stats_summary(rollup(recs, empty_totals()))
        self.assertEqual(
            payload["delegates"]["by_backend"],
            [{"id": "busy", "count": 2}, {"id": "quiet", "count": 1}],
        )

    def test_the_projection_survives_a_summary_missing_everything(self):
        # Degrade, don't fail: the panel's card is the last place a half-written store should
        # surface as a 500. Every field still answers with its zero value.
        payload = views.project_stats_summary({})
        self.assertEqual(payload["tasks"], 0)
        self.assertEqual(payload["by_model"], [])
        self.assertEqual(payload["by_day"], [])
        self.assertEqual(payload["delegates"]["linked_parents"], 0)


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
        # The rule is defensive: any author `display` on a view or its container beats the bare
        # `hidden` attribute — a UA rule — and paints both views at once. `.wrap`, the views'
        # immediate container, carries none, which is what keeps this belt-and-braces; `.main`
        # above it is a flex column, so the hazard is one rule away rather than hypothetical.
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


class StatusFooterTest(unittest.TestCase):
    """The persistent status footer (#188).

    Asserted against the shipped source for the reason `PanelLayoutTest` gives: there is no JS
    engine in this suite, and the alternative to reading the file is asserting nothing about the
    surface an operator actually looks at.

    What these guard is not styling. The footer exists because two signals that qualify every
    figure in the panel — the placeholder-pricing caveat and the measurement-health findings —
    rendered only inside the Settings stats card once the panel split into views, which put them
    a navigation click away from the view you land on. The failure mode is each of those signals
    quietly not arriving, in a strip that is invisible whenever it has nothing to say.
    """

    def setUp(self):
        self.panel = PANEL.read_text(encoding="utf-8")

    def test_the_footer_ships_and_starts_hidden(self):
        self.assertIn('id="statusbar"', self.panel)
        footer = re.search(r"<footer[^>]*id=\"statusbar\"[^>]*>", self.panel)
        self.assertIsNotNone(footer, "the status bar must be a <footer>, not a bare div")
        self.assertIn("hidden", footer.group(0), "an empty bar must not ship visible")

    def test_the_footer_is_a_live_region(self):
        # Findings can appear after a run, with the reader's attention on the output pane. A
        # polite live region announces that; a plain <footer> would change silently for anyone
        # not looking at the bottom of the screen.
        footer = re.search(r"<footer[^>]*id=\"statusbar\"[^>]*>", self.panel).group(0)
        self.assertIn('role="status"', footer)

    def test_the_hidden_attribute_can_actually_hide_it(self):
        # `[hidden] { display: none }` is a UA rule, so any author `display` on this element
        # outranks it and turns `hidden` into decoration — leaving the bar permanently visible
        # and, in the common case, permanently empty, which trains the reader to stop looking at
        # the one strip that only appears when something is wrong. The layout rules live on the
        # inner column precisely so the outer strip stays free of one; this pins the guard that
        # makes adding one safe, and nothing else in this suite would notice its absence.
        self.assertRegex(
            self.panel,
            r"\.statusbar\[hidden\]\s*\{[^}]*display:\s*none",
            "an author display rule on .statusbar must be undone for [hidden]",
        )

    def test_both_signals_are_routed_to_the_footer(self):
        # The two ends of the contract: the payload keys `view_stats` writes, and the call that
        # puts them in the bar. `test_the_panel_actually_renders_the_health_findings` pins the
        # health wire itself; this pins where it now terminates.
        # Anchored to the start of a line, so commenting the call out is a failure rather
        # than a substring that still matches. A plain `assertIn` passed against `// render…`.
        self.assertRegex(self.panel, r"(?m)^\s*renderStatusBar\(status\);")
        stats_fn = self.panel[self.panel.index("async function loadStats()"):]
        stats_fn = stats_fn[: stats_fn.index("async function loadRoster()")]
        self.assertIn("status.push", stats_fn)
        self.assertIn("d.is_placeholder", stats_fn)
        self.assertIn("d.health", stats_fn)

    def test_the_caveats_left_the_stats_card(self):
        # A move, not a copy. The footer is visible on the Settings view too, so leaving the
        # originals in place would show every caveat twice to the reader most likely to be
        # reading them.
        stats_fn = self.panel[self.panel.index("async function loadStats()"):]
        stats_fn = stats_fn[: stats_fn.index("async function loadRoster()")]
        self.assertNotIn('class="caveat">⚠ pricing:', stats_fn)
        self.assertNotIn('class="caveat">⚠ measurement:', stats_fn)

    def test_an_unreachable_store_is_not_rendered_as_a_clean_one(self):
        # The rule this repo already learned from the usage-log detector: silence renders
        # identically to health, in the one signal whose false all-clear is most expensive. A
        # failed /api/stats leaves the Settings card showing an error, but that card is on a view
        # the reader may not be on — so the footer must say so itself.
        self.assertRegex(self.panel, r"(?m)^\s*renderStatusBar\(null\);")
        # The message is the operator's to word (VRF-008 asks them), so this pins the property
        # rather than the phrasing: the null branch renders a visible item in the "unknown"
        # style, which is the muted one — "could not ask" must not paint like "asked, and it is
        # bad", and must not be nothing at all.
        self.assertRegex(self.panel, r'statusItem\(\s*"[^"]+",\s*"unknown"\s*\)')

    def test_the_bar_shares_the_content_column_s_geometry(self):
        # The strip spans the pane; its text must line up with the cards it qualifies. Both the
        # width and the inset have to agree, and disagreeing is silent — on a wide window the
        # items simply drift left of everything they annotate. Rather than assert two copies stay
        # equal, the geometry is declared once in `:root` and this pins that both columns read it:
        # a hard-coded value in either is the drift, before it can happen.
        for selector in (r"\.wrap", r"\.statusbar-inner"):
            block = re.search(selector + r"\s*\{([^}]*)\}", self.panel)
            self.assertIsNotNone(block, f"{selector} must exist")
            body = block.group(1)
            with self.subTest(selector=selector):
                self.assertIn("var(--content-max)", body, "width must come from the shared token")
                self.assertIn("var(--content-inset)", body, "inset must come from the shared token")
                self.assertNotRegex(
                    body, r"max-width:\s*\d",
                    "a literal max-width here is exactly the drift the token exists to prevent",
                )

        # And the text has to actually be put in that column. Writing to the outer strip's
        # innerHTML replaces the column element itself, which loses the alignment silently and
        # for good — the bar keeps working, just wrong, which is why the width check above
        # cannot see it.
        self.assertNotRegex(self.panel, r"(?m)^\s*bar\.innerHTML\s*=")
        self.assertRegex(self.panel, r"(?m)^\s*slot\.innerHTML\s*=")

    #: The statements in `renderStatusBar` whose ORDER is the behaviour, as (token, pattern).
    #: Matched in source order and compared as a sequence, because every interesting way to break
    #: this function leaves all of them present and only moves one.
    TOGGLE_STATEMENTS = (
        ("show", r"bar\.hidden = false;"),
        ("hide", r"bar\.hidden = true;"),
        ("fill-unknown", r"slot\.innerHTML = statusItem\("),
        ("clear", r'slot\.innerHTML = "";'),
        ("fill-items", r"slot\.innerHTML = items\.map"),
    )

    def _toggle_sequence(self):
        """Return the toggle/fill statements of `renderStatusBar`, in source order.

        Returns:
            list[str]: One token per matched statement, e.g. ``["show", "fill-unknown", ...]``.
        """
        start = self.panel.index("function renderStatusBar(")
        body = self.panel[start:]
        # Ends at the next top-level function, whichever it is: naming the neighbour meant that
        # inserting anything between the two silently widened the slice and the sequence with it.
        end = re.search(r"\n(?:async )?function ", body[1:])
        self.assertIsNotNone(end, "renderStatusBar must be followed by another function")
        body = body[: end.start() + 1]
        combined = re.compile("|".join(f"(?P<{tok.replace('-', '_')}>{pat})"
                                       for tok, pat in self.TOGGLE_STATEMENTS))
        return [m.lastgroup.replace("_", "-") for m in combined.finditer(body)]

    def test_the_bar_actually_becomes_visible(self):
        # Every other assertion in this class is about markup and CSS that a broken toggle would
        # leave untouched: drop or invert either line and the bar is stuck in one state forever
        # while the suite stays green.
        seq = self._toggle_sequence()
        self.assertIn("show", seq, "nothing ever unhides the bar")
        self.assertIn("hide", seq, "nothing ever hides the bar when there is nothing to report")

    def test_the_bar_is_shown_before_it_is_filled(self):
        # A live region populated while `display: none` is not announced by most assistive tech,
        # and unhiding an already-populated one is not reliably announced either — so filling
        # first silently costs exactly the transition `role="status"` was added for: a finding
        # appearing after a run. The order is the whole behaviour and it is invisible on screen,
        # so only this test and VRF-008 can catch a reversal.
        #
        # Asserted as a sequence rather than "a show appears before this fill": with two branches
        # there is always an earlier `show` to find, so a positional check passes against a
        # reversed branch. This pins each branch's own order.
        self.assertEqual(
            ["show", "fill-unknown", "clear", "hide", "show", "fill-items"],
            self._toggle_sequence(),
            "renderStatusBar's branches must each unhide before they write, and clear before they hide",
        )

    def test_a_200_that_is_not_a_stats_payload_is_not_read_as_data(self):
        # The last unsafe door into the invariant: a 200 whose body this code does not recognise
        # leaves `health` and `is_placeholder` undefined, so the findings list is empty and the
        # bar hides — "asked, and all clear" over a response nobody could read. Chunk 03 reshapes
        # this payload, which is what makes it worth a guard rather than a comment.
        self.assertRegex(self.panel, r'(?m)^\s*if \(!d \|\| typeof d !== "object"')
        self.assertIn('"summary" in d', self.panel)
        self.assertIn('"health" in d', self.panel)

    def test_a_failed_read_reports_what_the_server_said(self):
        # `server.py` composes `{"error": str(exc)}` and silences its own request logging, so that
        # body is the only rendering of the cause anywhere. Throwing on the status alone leaves a
        # bare number, and VRF-008's corrupt-pricing step walks straight into it.
        self.assertIn("body.error", self.panel)
        self.assertRegex(self.panel, r"(?m)^\s*console\.error\(\"roster:\", e\);")
        self.assertRegex(self.panel, r"(?m)^\s*console\.error\(\"pricing:\", e\);")

    def test_an_error_response_is_not_read_as_data(self):
        # `fetch` resolves on 500, and this server answers a failed read view with a well-formed
        # `{"error": ...}` body — not a hypothesis: `DispatchTest.test_read_view_error_is_clean_
        # json_500` pins that it does. Without an `r.ok` check that body parses and every consumer
        # reads it as data — the status bar sees no findings and hides, which is the one state it
        # exists to prevent. Pinned at `getJSON` because all four consumers pass through it; the
        # two tests together are as close as a JS-engine-free suite gets to the round trip.
        self.assertRegex(self.panel, r"(?m)^\s*if \(!r\.ok\) \{")
        self.assertRegex(self.panel, r"(?m)^\s*throw new Error\(`\$\{url\} answered ")

    def test_the_failure_path_leaves_a_trace(self):
        # The bar states what the code knows — the read failed — and the console carries why.
        # Without it an unexpected payload shape reads to the operator as a dead server.
        self.assertRegex(self.panel, r'(?m)^\s*console\.error\("stats:", e\);')

    def test_the_footer_does_not_overlap_the_panes_it_annotates(self):
        # `position: fixed` would cover the sidebar and sit on top of the scrolling content;
        # sticky inside the centre pane scrolls with it and stops at its foot. The Car's second
        # Key Constraint is exactly this, and it is one CSS word away from being violated.
        bar = re.search(r"\.statusbar\s*\{([^}]*)\}", self.panel)
        self.assertIsNotNone(bar)
        self.assertIn("position: sticky", bar.group(1))
        self.assertNotIn("position: fixed", bar.group(1))


class SpendChartsTest(unittest.TestCase):
    """The per-model table and the per-day sparkline, asserted against the shipped source.

    Same technique and same reason as `PanelLayoutTest`: no JS engine runs here, and the
    alternative to reading the file is asserting nothing about the surface an operator looks at.
    So these are deliberately narrow — they pin the *contract* between the payload and the
    renderer, and the two rules that would be silently wrong rather than visibly broken: that the
    chart draws from the series and not from the coverage stamp, and that every value it draws is
    also readable as text.

    What they cannot cover is geometry and interaction. The point arithmetic was verified by
    running these functions against a live `/api/stats` payload, and the rendered result by
    screenshot; the window buttons are a keyboard-and-mouse path that only a person can sign off,
    which is what the operator-verification entry exists for.
    """

    def setUp(self):
        self.panel = PANEL.read_text(encoding="utf-8")
        self.chart = self.panel[self.panel.index("function chartPlot("):self.panel.index("function modelTable(")]
        # Everything that turns the series into a drawing: the point arithmetic as well as the
        # function that assembles the SVG. Slicing only the assembler would leave the assertion
        # about the drawable boundary pointing at code that does no geometry.
        self.drawing = (
            self.panel[self.panel.index("function sparkPoints("):self.panel.index("function dayTwin(")]
            + self.chart
        )

    def test_the_panel_reads_every_field_the_projection_adds(self):
        # The wire between the two ends. `view_stats` emitting a field and the panel rendering one
        # are each covered; renaming either side is what nothing would catch.
        stats_fn = self.panel[self.panel.index("async function loadStats()"):]
        stats_fn = stats_fn[: stats_fn.index("async function loadRoster()")]
        for key in ("s.by_model", "s.by_day", "s.by_day_since", "s.spend_avoided_outside_days_usd"):
            # Word-bounded: `s.by_day` is a strict prefix of `s.by_day_since`, so a plain substring
            # check for the series key passes on the stamp alone — the one rename it exists to
            # catch would sail through it.
            self.assertRegex(
                stats_fn, rf"{re.escape(key)}\b(?!_)",
                f"the panel must read the payload key view_stats writes: {key}",
            )
        self.assertIn("dg.linked_parents", stats_fn)
        self.assertIn("dg.by_backend", stats_fn)

    def test_the_chart_draws_from_the_series_not_from_the_coverage_stamp(self):
        # The one rule most likely to be got wrong, and it fails silently: `by_day_since` is older
        # than the oldest surviving bucket once eviction starts, so a chart that began there would
        # paint the evicted span as $0 — asserting quiet days over days that were merely not kept.
        # The stamp belongs to the caption's wording alone.
        self.assertNotIn("by_day_since", self.drawing)
        self.assertNotIn("chartData.since", self.drawing)
        caption = self.panel[self.panel.index("function chartCaption("):self.panel.index("function chartMarkup()")]
        self.assertIn("chartData.since", caption, "the caption is where the stamp belongs")

    def test_the_window_control_offers_the_three_windows_and_starts_at_thirty(self):
        self.assertIn("const CHART_WINDOWS = [7, 30, 90];", self.panel)
        self.assertRegex(self.panel, r"(?m)^let chartWindow = 30;")
        # aria-pressed, not a class: the selected window must be announced, not only painted.
        self.assertIn('aria-pressed="${n === chartWindow}"', self.panel)
        self.assertIn('role="group" aria-label="Chart window"', self.panel)

    def test_the_server_ships_every_day_the_widest_window_draws(self):
        # One rule in two languages: the endpoint sends the widest window the panel offers. Both
        # sites say so in a comment and neither could enforce it — a fourth button at 180 days
        # would have drawn 90 days of chart under a "180d" label, with every test green.
        windows = re.search(r"const CHART_WINDOWS = \[([^\]]+)\];", self.panel)
        self.assertIsNotNone(windows)
        widest = max(int(n) for n in windows.group(1).split(","))
        self.assertLessEqual(
            widest, views.STATS_DAY_WINDOW,
            "the panel offers a window wider than /api/stats sends; widen STATS_DAY_WINDOW or "
            "narrow CHART_WINDOWS — the label would otherwise promise days the payload lacks",
        )

    def test_changing_the_window_re_renders_without_refetching(self):
        # A window change is a view choice over data already in hand. Refetching would re-probe
        # the store and re-announce the status bar's live region for nothing.
        handler = self.panel[self.panel.index('$("statsCard").addEventListener'):]
        handler = handler[: handler.index("showView(")]
        self.assertNotIn("loadStats()", handler)
        # The buttons live outside the replaced region, so the reader's focus survives the press.
        self.assertIn("aria-pressed", handler)
        # Order is the behaviour here, so the ordered sequence is what is asserted — a positional
        # "X appears before Y" passes with the two swapped as long as both are present. The chart
        # is rendered from the chosen window BEFORE the state or the buttons move, so a throw
        # leaves all three agreeing on the old window rather than two of them claiming the new one.
        steps = re.findall(
            r"(chartPlot\(chosen\)|chartWindow = chosen|plot\.innerHTML = html|aria-pressed|console\.error)",
            handler,
        )
        self.assertEqual(
            steps,
            ["chartPlot(chosen)", "chartWindow = chosen", "plot.innerHTML = html", "aria-pressed",
             "console.error"],
            "render, then commit the state, then paint it — and report a failure on the panel's "
            "one debugging channel",
        )

    def test_every_drawn_value_is_also_readable_as_text(self):
        # The chart ships no tooltip, so without the twin a figure would be reachable only as a
        # shape. Both the SVG's label and the table are structural, not decoration.
        self.assertIn('role="img"', self.chart)
        self.assertIn('aria-label="${esc(label)}"', self.chart)
        self.assertIn('<details class="twin">', self.panel)
        self.assertIn("dayTwin(shown)", self.chart)

    def test_an_empty_store_says_so_rather_than_drawing_a_flat_line(self):
        # A store with no per-day history is the common case on a fresh upgrade. Drawing it as a
        # month of $0 would assert no activity over a period that simply was not bucketed.
        self.assertIn("No per-day figures recorded yet", self.chart)
        # Both branches of chartPlot return a caption or a statement — never an empty chart frame.
        returns = re.findall(r"(?m)^\s*return (.+?);\s*$", self.chart)
        self.assertEqual(len(returns), 2, f"chartPlot should have exactly two exits, found {returns}")
        self.assertTrue(all("caption" in r.lower() for r in returns), returns)

    def test_a_store_with_no_series_offers_no_window_controls(self):
        # Three buttons that visibly do nothing are worse than none: on a fresh install the chart
        # has nothing to draw, and a window control there invites a click it answers with the same
        # sentence. The check is in chartMarkup, which owns the head; chartPlot owns the message.
        markup = self.panel[self.panel.index("function chartMarkup()"):self.panel.index("function chartPlot(")]
        self.assertRegex(
            markup,
            r"(?m)^\s*if \(!\(chartData\.series \|\| \[\]\)\.length\) return ",
            "the empty case must return before the buttons are built",
        )

    def test_the_model_table_never_encodes_a_value_in_the_bar_alone(self):
        table_fn = self.panel[self.panel.index("function modelTable("):self.panel.index("async function loadStats()")]
        # The dollar figure is a cell of its own; the bar restates it and is hidden from assistive
        # tech so it is not announced twice.
        self.assertIn("money(spend)", table_fn)
        self.assertIn('aria-hidden="true"', table_fn)
        self.assertIn("tabular-nums", self.panel, "columns of figures must align")

    def test_the_chart_ships_no_library_and_no_off_machine_reference(self):
        # The offline guarantee, at the one place a chart would normally break it. The panel-wide
        # guard in PanelLayoutTest covers the file; this says the intent out loud at the surface
        # that would have wanted a CDN.
        for banned in ("chart.js", "cdn.", "unpkg", "jsdelivr", "d3."):
            self.assertNotIn(banned, self.panel.lower())
        self.assertIn("<svg", self.chart, "the chart is hand-built inline SVG")


if __name__ == "__main__":
    unittest.main()
