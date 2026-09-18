#!/usr/bin/env python3
"""Tests for the mirror freshness audit: classification, reporting, issue sync."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mirror_freshness as mf

NOW = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
MAX_PR_AGE = mf.DEFAULT_MAX_PULL_REQUEST_AGE_HOURS
MAX_LAG = mf.DEFAULT_MAX_LAG_HOURS


def pull(number, created_at, author="pull[bot]", title="[pull] master from upstream:master", state="open"):
    return {"number": number, "created_at": created_at, "state": state, "title": title,
            "user": {"login": author}}


def mirror(ahead=0, behind=0, lag=1.0, pr=None, repository="joshuadavidthomas/hiwave"):
    return {"repository": repository, "branch": "master", "upstream": "upstream/project",
            "pushed_at": "2026-09-18T09:00:00Z", "lag_hours": lag, "status": None,
            "ahead_by": ahead, "behind_by": behind, "sync_pull_request": pr, "error": None}


def kinds(entry):
    return [reason["kind"] for reason in mf.evaluate_mirror(entry, MAX_PR_AGE, MAX_LAG)]


class FakeClient:
    """Stands in for GitHub, serving the payload shapes the real API returns."""

    def __init__(self, forks, details, compares, pulls, issues=None):
        self.forks = forks
        self.details = details
        self.compares = compares
        self.pulls = pulls
        self.issues = issues or []
        self.calls = []

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        if "/compare/" in path:
            return self.compares[path.split("/compare/")[0][len("/repos/"):]]
        if path.startswith("/repos/") and path.count("/") == 3:
            name = path[len("/repos/"):]
            if name not in self.details:
                raise mf.GitHubError(f"GET {path} -> HTTP 404: Not Found")
            return self.details[name]
        return {"number": 42}

    def paginate(self, path, params=None, max_pages=10):
        if path.startswith("/users/"):
            return self.forks
        if path.endswith("/pulls"):
            return self.pulls.get(path[len("/repos/"):-len("/pulls")], [])
        if path.endswith("/issues"):
            return self.issues
        raise AssertionError(f"unexpected path {path}")


HIWAVE_DETAIL = {"full_name": "joshuadavidthomas/hiwave", "default_branch": "master",
                 "pushed_at": "2026-09-18T08:19:30Z",
                 "parent": {"full_name": "hiwavebrowser/hiwave", "default_branch": "master"}}
PI_MONO_DETAIL = {"full_name": "joshuadavidthomas/pi-mono", "default_branch": "main",
                  "pushed_at": "2026-09-17T23:46:10Z",
                  "parent": {"full_name": "earendil-works/pi", "default_branch": "main"}}
TOKYONIGHT_DETAIL = {"full_name": "joshuadavidthomas/tokyonight.nvim", "default_branch": "josh",
                     "pushed_at": "2026-09-18T00:00:00Z",
                     "parent": {"full_name": "folke/tokyonight.nvim", "default_branch": "main"}}


class DetectionTests(unittest.TestCase):
    def test_sync_pull_request_detection(self):
        self.assertTrue(mf.is_sync_pull_request(pull(1, "2026-01-14T12:57:28Z")))
        self.assertTrue(mf.is_sync_pull_request(
            {"number": 7, "title": "[pull] main from x:main", "user": {"login": "someone"}}))
        self.assertFalse(mf.is_sync_pull_request(
            pull(9, "2026-09-18T00:00:00Z", author="joshuadavidthomas", title="Fix the thing")))
        self.assertFalse(mf.is_sync_pull_request({"number": 2, "title": None, "user": None}))

    def test_oldest_open_sync_pull_request_skips_closed_and_other_bots(self):
        pulls = [
            pull(13, "2026-09-17T17:19:38Z"),
            pull(2, "2026-01-14T12:57:28Z"),
            pull(4, "2026-05-01T00:00:00Z", author="dependabot[bot]", title="Bump foo"),
            pull(5, "2026-01-01T00:00:00Z", state="closed"),
        ]
        age, number = mf.oldest_open_sync_pull_request(pulls, NOW)
        self.assertEqual(number, 2)
        self.assertAlmostEqual(age, 5925.0, delta=2.0)

    def test_oldest_open_sync_pull_request_absent(self):
        self.assertIsNone(mf.oldest_open_sync_pull_request(
            [pull(4, "2026-05-01T00:00:00Z", author="dependabot[bot]", title="Bump foo")], NOW))

    def test_non_mirror_reason(self):
        self.assertIsNone(mf.non_mirror_reason(HIWAVE_DETAIL))
        self.assertIn("differs", mf.non_mirror_reason(TOKYONIGHT_DETAIL))
        self.assertIn("no upstream", mf.non_mirror_reason({"default_branch": "main", "parent": None}))

    def test_format_hours(self):
        self.assertEqual(mf.format_hours(None), "unknown")
        self.assertEqual(mf.format_hours(17.0), "17.0h")
        self.assertEqual(mf.format_hours(5926.0), "246.9d")

    def test_diverged_mirror_is_stalled(self):
        # joshuadavidthomas/hiwave: 282 commits upstream lacks, sync PR open since 2026-01-14.
        entry = mirror(ahead=282, behind=737, lag=1.7, pr={"number": 2, "age_hours": 5926.0})
        self.assertEqual(kinds(entry), ["diverged", "stale_sync_pull_request"])

    def test_diverged_mirror_with_young_sync_pr_is_stalled(self):
        # joshuadavidthomas/djangopackages: 2 commits upstream lacks, sync PR open 17 hours.
        entry = mirror(ahead=2, behind=15, lag=2.7, pr={"number": 13, "age_hours": 17.0})
        self.assertEqual(kinds(entry), ["diverged", "stale_sync_pull_request"])

    def test_recently_behind_mirror_is_not_stalled(self):
        # pi-mono at detection: 34 commits behind, last push 11 hours ago, normal bot latency.
        self.assertEqual(kinds(mirror(ahead=0, behind=34, lag=11.0)), [])

    def test_long_lag_without_a_sync_pull_request_is_stalled(self):
        # django-rusty-templates at detection: 4 commits behind, nothing pushed for 53 hours.
        self.assertEqual(kinds(mirror(ahead=0, behind=4, lag=53.0)), ["no_recent_sync"])

    def test_level_mirror_is_not_stalled(self):
        self.assertEqual(kinds(mirror(ahead=0, behind=0, lag=0.1)), [])

    def test_ahead_with_nothing_pending_is_not_stalled(self):
        self.assertEqual(kinds(mirror(ahead=4168, behind=0, lag=1.0)), [])

    def test_young_sync_pull_request_is_not_stalled(self):
        self.assertEqual(kinds(mirror(ahead=0, behind=3, pr={"number": 8, "age_hours": 2.5})), [])


class AuditTests(unittest.TestCase):
    def fleet(self):
        forks = [
            {"full_name": "joshuadavidthomas/hiwave", "fork": True},
            {"full_name": "joshuadavidthomas/pi-mono", "fork": True},
            {"full_name": "joshuadavidthomas/tokyonight.nvim", "fork": True},
            {"full_name": "joshuadavidthomas/djangodocs.org", "fork": False},
        ]
        details = {
            "joshuadavidthomas/hiwave": HIWAVE_DETAIL,
            "joshuadavidthomas/pi-mono": PI_MONO_DETAIL,
            "joshuadavidthomas/tokyonight.nvim": TOKYONIGHT_DETAIL,
        }
        compares = {
            "joshuadavidthomas/hiwave": {"status": "diverged", "ahead_by": 282, "behind_by": 737},
            "joshuadavidthomas/pi-mono": {"status": "behind", "ahead_by": 0, "behind_by": 34},
        }
        pulls = {"joshuadavidthomas/hiwave": [pull(2, "2026-01-14T12:57:28Z")]}
        return FakeClient(forks, details, compares, pulls)

    def test_audit_finds_the_stalled_mirror_and_explains_the_rest(self):
        result = mf.audit(self.fleet(), "joshuadavidthomas", now=NOW)
        self.assertTrue(result["stalled"])
        self.assertEqual(result["forks_seen"], 3)
        self.assertEqual(result["mirrors_checked"], 2)
        self.assertEqual([entry["repository"] for entry in result["stalls"]], ["joshuadavidthomas/hiwave"])
        self.assertEqual([reason["kind"] for reason in result["stalls"][0]["reasons"]],
                         ["diverged", "stale_sync_pull_request"])
        self.assertEqual(result["advisories"], [])
        self.assertEqual([entry["repository"] for entry in result["skipped"]], ["joshuadavidthomas/tokyonight.nvim"])
        self.assertIn("differs", result["skipped"][0]["reason"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(mf.exit_code(result), 1)
        self.assertIn("joshuadavidthomas/hiwave", mf.summarize(result))

    def test_audit_of_a_healthy_fleet_is_green(self):
        client = FakeClient(
            [{"full_name": "joshuadavidthomas/pi-mono", "fork": True}],
            {"joshuadavidthomas/pi-mono": PI_MONO_DETAIL},
            {"joshuadavidthomas/pi-mono": {"status": "behind", "ahead_by": 0, "behind_by": 34}},
            {},
        )
        result = mf.audit(client, "joshuadavidthomas", now=NOW)
        self.assertFalse(result["stalled"])
        self.assertEqual(mf.exit_code(result), 0)

    def test_unreadable_mirror_is_not_green(self):
        client = FakeClient([{"full_name": "joshuadavidthomas/hiwave", "fork": True}], {}, {}, {})
        client.details = {}
        try:
            result = mf.audit(client, "joshuadavidthomas", now=NOW)
        except KeyError:
            self.fail("audit must record an unreadable repository instead of raising")
        self.assertEqual(result["mirrors_checked"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(mf.exit_code(result), 1)

    def test_ignored_and_selected_repositories(self):
        result = mf.audit(self.fleet(), "joshuadavidthomas", ignore=["joshuadavidthomas/hiwave"], now=NOW)
        self.assertFalse(result["stalled"])
        self.assertIn("ignored by configuration", [entry["reason"] for entry in result["skipped"]])

        result = mf.audit(self.fleet(), "joshuadavidthomas",
                          repositories=["joshuadavidthomas/hiwave"], now=NOW)
        self.assertTrue(result["stalled"])
        self.assertEqual(result["forks_seen"], 1)

    def test_report_and_issue_body_name_the_stalled_mirror(self):
        result = mf.audit(self.fleet(), "joshuadavidthomas", now=NOW)
        report = mf.render_markdown(result)
        self.assertIn("| `joshuadavidthomas/hiwave` |", report)
        self.assertIn("246.9d", report)
        body = mf.build_issue_body(result)
        self.assertIn("joshuadavidthomas/hiwave", body)
        self.assertIn("closed", body)


class TrackingIssueTests(unittest.TestCase):
    def stalled_result(self, issues=None):
        return mf.audit(FakeClient(
            [{"full_name": "joshuadavidthomas/hiwave", "fork": True}],
            {"joshuadavidthomas/hiwave": HIWAVE_DETAIL},
            {"joshuadavidthomas/hiwave": {"status": "diverged", "ahead_by": 282, "behind_by": 737}},
            {"joshuadavidthomas/hiwave": [pull(2, "2026-01-14T12:57:28Z")]},
        ), "joshuadavidthomas", now=NOW)

    def test_opens_the_tracking_issue(self):
        client = FakeClient([], {}, {}, {}, issues=[])
        outcome = mf.sync_tracking_issue(client, "joshuadavidthomas/.github", self.stalled_result())
        self.assertEqual(outcome, {"action": "opened", "number": 42})
        self.assertEqual(client.calls[0][0], "POST")
        self.assertEqual(client.calls[0][1], "/repos/joshuadavidthomas/.github/issues")
        self.assertEqual(client.calls[0][2]["title"], mf.TRACKING_ISSUE_TITLE)

    def test_updates_the_existing_tracking_issue(self):
        client = FakeClient([], {}, {}, {}, issues=[{"number": 5, "title": mf.TRACKING_ISSUE_TITLE}])
        outcome = mf.sync_tracking_issue(client, "joshuadavidthomas/.github", self.stalled_result())
        self.assertEqual(outcome, {"action": "updated", "number": 5})
        self.assertEqual(client.calls[0][0], "PATCH")

    def test_closes_the_issue_once_every_mirror_is_level(self):
        result = self.stalled_result()
        result["stalled"] = False
        client = FakeClient([], {}, {}, {}, issues=[{"number": 5, "title": mf.TRACKING_ISSUE_TITLE}])
        outcome = mf.sync_tracking_issue(client, "joshuadavidthomas/.github", result)
        self.assertEqual(outcome, {"action": "closed", "number": 5})
        self.assertEqual([call[0] for call in client.calls], ["POST", "PATCH"])
        self.assertEqual(client.calls[1][2], {"state": "closed"})

    def test_does_nothing_when_there_is_no_issue_and_no_stall(self):
        result = self.stalled_result()
        result["stalled"] = False
        client = FakeClient([], {}, {}, {}, issues=[])
        self.assertEqual(mf.sync_tracking_issue(client, "joshuadavidthomas/.github", result),
                         {"action": "none", "number": None})
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
