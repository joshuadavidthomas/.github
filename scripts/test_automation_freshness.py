#!/usr/bin/env python3
"""Tests for the automation freshness audit: classification, reporting, exit code."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import automation_freshness as af

NOW = datetime(2026, 9, 21, 23, 0, tzinfo=timezone.utc)
MIN_AGE = af.DEFAULT_MIN_APPROVAL_AGE_HOURS
LOOKBACK = af.DEFAULT_LOOKBACK_DAYS


def workflow(name, state="active", path=".github/workflows/x.yml"):
    return {"name": name, "state": state, "path": path}


def run(number, created_at, name="lint", branch="feature"):
    return {"name": name, "run_number": number, "created_at": created_at, "head_branch": branch,
            "status": "completed", "conclusion": "action_required"}


class FakeClient:
    """Stands in for GitHub, serving the payload shapes the real API returns."""

    def __init__(self, repos, details, workflows, runs, unreadable=()):
        self.repos = repos
        self.details = details
        self.workflows = workflows
        self.runs = runs
        self.unreadable = {name.lower() for name in unreadable}
        self.collection_calls = []

    def request(self, method, path, params=None, body=None):
        if path.startswith("/repos/") and path.count("/") == 3:
            name = path[len("/repos/"):]
            if name.lower() in self.unreadable or name not in self.details:
                raise af.GitHubError(f"GET {path} -> HTTP 404: Not Found")
            return self.details[name]
        raise AssertionError(f"unexpected request {method} {path}")

    def paginate(self, path, params=None, max_pages=10):
        if path.startswith("/users/"):
            return self.repos
        raise AssertionError(f"unexpected paginate {path}")

    def paginate_collection(self, path, key, params=None, max_pages=10):
        self.collection_calls.append((path, dict(params or {})))
        if path.endswith("/actions/workflows"):
            name = path[len("/repos/"):-len("/actions/workflows")]
            if name.lower() in self.unreadable:
                raise af.GitHubError(f"GET {path} -> HTTP 403: Forbidden")
            return self.workflows.get(name, [])
        if path.endswith("/actions/runs"):
            name = path[len("/repos/"):-len("/actions/runs")]
            return self.runs.get(name, [])
        raise AssertionError(f"unexpected collection {path}")


OPPY = "joshuadavidthomas/oppy-os"
KBD = "joshuadavidthomas/kbd"
OPENCODE = "joshuadavidthomas/opencode"


def fleet():
    return FakeClient(
        repos=[{"full_name": OPPY, "fork": False},
               {"full_name": KBD, "fork": False},
               {"full_name": OPENCODE, "fork": True}],
        details={},
        workflows={
            OPPY: [workflow("build-ublue-custom", state="disabled_inactivity",
                            path=".github/workflows/build.yml")],
            KBD: [workflow("lint"), workflow("test"), workflow("deny")],
            OPENCODE: [workflow("stats", state="disabled_inactivity")],
        },
        runs={
            KBD: [run(166, "2026-09-21T21:33:03Z", name="semver",
                      branch="release-plz-2026-09-21T21-32-57Z")],
        },
    )


class FormatTests(unittest.TestCase):
    def test_format_hours(self):
        self.assertEqual(af.format_hours(None), "unknown")
        self.assertEqual(af.format_hours(1.0), "1.0h")
        self.assertEqual(af.format_hours(72.0), "3.0d")

    def test_parse_timestamp(self):
        self.assertEqual(af.parse_timestamp("2026-09-21T21:33:03Z"),
                         datetime(2026, 9, 21, 21, 33, 3, tzinfo=timezone.utc))
        self.assertIsNone(af.parse_timestamp(None))
        self.assertIsNone(af.parse_timestamp("not-a-date"))


class EvaluationTests(unittest.TestCase):
    def test_disabled_workflow_is_a_reason(self):
        entry = {"disabled_workflows": [{"name": "build-ublue-custom", "path": "build.yml",
                                         "state": "disabled_inactivity"}],
                 "unapproved_runs": []}
        reasons = af.evaluate_repository(entry)
        self.assertEqual([r["kind"] for r in reasons], ["disabled_workflow"])
        self.assertIn("disabled_inactivity", reasons[0]["detail"])

    def test_unapproved_run_is_a_reason(self):
        entry = {"disabled_workflows": [],
                 "unapproved_runs": [{"workflow": "semver", "number": 166, "branch": "release",
                                      "age_hours": 10.0}]}
        reasons = af.evaluate_repository(entry)
        self.assertEqual([r["kind"] for r in reasons], ["unapproved_run"])
        self.assertIn("#166", reasons[0]["detail"])

    def test_active_workflow_and_no_runs_is_clean(self):
        entry = {"disabled_workflows": [], "unapproved_runs": []}
        self.assertEqual(af.evaluate_repository(entry), [])


class AuditTests(unittest.TestCase):
    def test_disabled_workflow_on_owned_repo_is_a_stall(self):
        result = af.audit(fleet(), "joshuadavidthomas", now=NOW)
        self.assertTrue(result["stalled"])
        oppy = [e for e in result["stalls"] if e["repository"] == OPPY][0]
        self.assertEqual([r["kind"] for r in oppy["reasons"]], ["disabled_workflow"])
        self.assertEqual(result["repositories_seen"], 3)
        self.assertEqual(result["repositories_checked"], 3)

    def test_unapproved_run_on_owned_repo_is_a_stall(self):
        result = af.audit(fleet(), "joshuadavidthomas", now=NOW)
        kbd = [e for e in result["stalls"] if e["repository"] == KBD]
        self.assertEqual(len(kbd), 1)
        self.assertEqual([r["kind"] for r in kbd[0]["reasons"]], ["unapproved_run"])
        self.assertIn("release-plz", kbd[0]["reasons"][0]["detail"])

    def test_disabled_workflow_on_fork_is_an_advisory_not_a_stall(self):
        result = af.audit(fleet(), "joshuadavidthomas", now=NOW)
        self.assertEqual([e["repository"] for e in result["advisories"]], [OPENCODE])
        self.assertNotIn(OPENCODE, [e["repository"] for e in result["stalls"]])

    def test_recent_unapproved_run_is_held_back(self):
        client = fleet()
        client.runs = {
            KBD: [run(1, "2026-09-21T22:50:00Z")],  # 10 minutes old, below the grace period
        }
        result = af.audit(client, "joshuadavidthomas", now=NOW)
        self.assertNotIn(KBD, [e["repository"] for e in result["stalls"]])

    def test_run_query_asks_for_action_required_with_a_cutoff(self):
        client = fleet()
        af.audit(client, "joshuadavidthomas", lookback_days=30, now=NOW)
        run_calls = [params for path, params in client.collection_calls if path.endswith("/actions/runs")]
        self.assertTrue(run_calls)
        for params in run_calls:
            self.assertEqual(params["status"], "action_required")
            self.assertTrue(params["created"].startswith(">="))
            self.assertTrue(params["created"].endswith("Z"))

    def test_healthy_fleet_is_green(self):
        client = FakeClient(
            repos=[{"full_name": KBD, "fork": False}],
            details={},
            workflows={KBD: [workflow("lint"), workflow("test")]},
            runs={},
        )
        result = af.audit(client, "joshuadavidthomas", now=NOW)
        self.assertFalse(result["stalled"])
        self.assertEqual(af.exit_code(result), 0)

    def test_unreadable_repository_is_recorded_and_not_green(self):
        client = FakeClient(repos=[{"full_name": OPPY, "fork": False}], details={}, workflows={}, runs={},
                            unreadable=[OPPY])
        result = af.audit(client, "joshuadavidthomas", now=NOW)
        self.assertEqual(result["repositories_checked"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(af.exit_code(result), 1)

    def test_ignored_repository_is_skipped(self):
        result = af.audit(fleet(), "joshuadavidthomas", ignore=[OPPY, KBD], now=NOW)
        self.assertFalse(result["stalled"])
        self.assertEqual([e["reason"] for e in result["skipped"]], ["ignored by configuration"] * 2)

    def test_explicit_repository_reads_fork_flag_from_repo_detail(self):
        client = FakeClient(repos=[], details={OPENCODE: {"full_name": OPENCODE, "fork": True}},
                            workflows={OPENCODE: [workflow("stats", state="disabled_inactivity")]},
                            runs={})
        result = af.audit(client, "joshuadavidthomas", repositories=[OPENCODE], now=NOW)
        self.assertEqual(result["repositories_seen"], 1)
        self.assertEqual([e["repository"] for e in result["advisories"]], [OPENCODE])
        self.assertFalse(result["stalled"])


class ReportingTests(unittest.TestCase):
    def test_summary_and_report_name_the_stalls(self):
        result = af.audit(fleet(), "joshuadavidthomas", now=NOW)
        summary = af.summarize(result)
        self.assertIn("STOPPED " + OPPY, summary)
        self.assertIn("STOPPED " + KBD, summary)
        report = af.render_markdown(result)
        self.assertIn("| `" + OPPY + "` |", report)
        self.assertIn("disabled_workflow", report)
        self.assertIn("Fork mirrors (incidental)", report)
        self.assertIn(OPENCODE, report)
        self.assertEqual(af.exit_code(result), 1)

    def test_clean_report_says_none(self):
        client = FakeClient(repos=[{"full_name": KBD, "fork": False}], details={},
                            workflows={KBD: [workflow("lint")]}, runs={})
        result = af.audit(client, "joshuadavidthomas", now=NOW)
        report = af.render_markdown(result)
        self.assertIn("None. Every enumerated workflow is active", report)
        self.assertNotIn("| Repository | Signal | Detail |", report)


if __name__ == "__main__":
    unittest.main()
