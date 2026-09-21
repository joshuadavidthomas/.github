#!/usr/bin/env python3
"""Report GitHub Actions automation that has silently stopped in an installation.

Automation here stops in two ways that leave no signal, and nothing watches for
either.

* ``disabled_workflow`` -- a workflow GitHub switched off, almost always with
  state ``disabled_inactivity`` after the repository saw no activity for 60
  days. The workflow's schedules and bots simply stop running. GitHub emails the
  owner once and nothing else records it, so the first place it is noticed is a
  manual sweep.
* ``unapproved_run`` -- a run that was created but never executed, parked in
  conclusion ``action_required`` because a pull-request run is waiting for an
  approval nobody has given. The pull request then merges with none of its
  checks having run.

This installation requires no status check to merge (tracked separately), so a
run that never executes and a workflow that was switched off are
indistinguishable from success: the merge goes ahead and the schedule is simply
gone. This script reports both so they can be seen and acted on.

It reads public repository data only, so it needs no credential beyond the
workflow's own token; a repository it cannot read is reported, never skipped, so
a green run means every enumerated repository was read. Workflows GitHub
disabled on a fork mirror are reported separately as incidental: the mirror's
schedule is nobody's here, and mirror syncing is tracked on its own.

Exit status is 1 when an owned repository has a stall, or when any repository
could not be read, and 0 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_ROOT = "https://api.github.com"
DEFAULT_OWNER = "joshuadavidthomas"
ACTIVE_STATE = "active"
DISABLED_INACTIVITY = "disabled_inactivity"
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_MIN_APPROVAL_AGE_HOURS = 1.0


class GitHubError(RuntimeError):
    """A GitHub API call that did not return a usable response."""


class GitHub:
    """Minimal read client for the handful of endpoints this check needs."""

    def __init__(self, token=None, api_root=API_ROOT, timeout=30):
        self.token = token
        self.api_root = api_root.rstrip("/")
        self.timeout = timeout

    def request(self, method, path, params=None, body=None):
        url = self.api_root + path
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "automation-freshness")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace").strip()
            raise GitHubError(f"{method} {path} -> HTTP {error.code}: {detail[:200]}") from error
        except urllib.error.URLError as error:
            raise GitHubError(f"{method} {path} -> {error.reason}") from error
        if not payload:
            return None
        return json.loads(payload)

    def paginate(self, path, params=None, max_pages=10):
        """Read a paginated endpoint that returns a bare JSON list."""
        params = dict(params or {})
        per_page = int(params.get("per_page", 100))
        items = []
        for page in range(1, max_pages + 1):
            params["page"] = page
            chunk = self.request("GET", path, params=params)
            if not isinstance(chunk, list):
                raise GitHubError(f"GET {path} did not return a list")
            items.extend(chunk)
            if len(chunk) < per_page:
                break
        return items

    def paginate_collection(self, path, key, params=None, max_pages=10):
        """Read a paginated endpoint that wraps its items in an object key."""
        params = dict(params or {})
        per_page = int(params.get("per_page", 100))
        items = []
        for page in range(1, max_pages + 1):
            params["page"] = page
            payload = self.request("GET", path, params=params)
            if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
                raise GitHubError(f"GET {path} did not return a {key!r} list")
            chunk = payload[key]
            items.extend(chunk)
            if len(chunk) < per_page:
                break
        return items


def parse_timestamp(value):
    """Parse a GitHub ISO-8601 timestamp into an aware datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def hours_between(earlier, later):
    if earlier is None or later is None:
        return None
    return (later - earlier).total_seconds() / 3600.0


def format_hours(hours):
    if hours is None:
        return "unknown"
    if abs(hours) >= 48:
        return f"{hours / 24:.1f}d"
    return f"{hours:.1f}h"


def evaluate_repository(entry):
    """Return the stall reasons for one repository. Pure function."""
    reasons = []
    for workflow in entry["disabled_workflows"]:
        reasons.append({
            "kind": "disabled_workflow",
            "detail": f"workflow {workflow['name']!r} ({workflow['path']}) is {workflow['state']}",
        })
    for run in entry["unapproved_runs"]:
        reasons.append({
            "kind": "unapproved_run",
            "detail": (
                f"run #{run['number']} of {run['workflow']!r} on {run['branch']!r} has awaited "
                f"approval for {format_hours(run['age_hours'])}"
            ),
        })
    return reasons


def audit(client, owner, repositories=None, ignore=(), lookback_days=DEFAULT_LOOKBACK_DAYS,
          min_approval_age_hours=DEFAULT_MIN_APPROVAL_AGE_HOURS, now=None):
    """Audit the owner's repositories and return a JSON-serialisable result."""
    now = now or datetime.now(timezone.utc)
    ignored = {name.lower() for name in ignore}
    cutoff = (now - timedelta(days=lookback_days)).isoformat().replace("+00:00", "Z")

    if repositories:
        candidates = [{"full_name": name, "fork": None} for name in repositories]
    else:
        repos = client.paginate(f"/users/{owner}/repos", {"type": "owner", "per_page": 100})
        candidates = [
            {"full_name": repo.get("full_name") or f"{owner}/{repo.get('name')}",
             "fork": bool(repo.get("fork"))}
            for repo in repos
        ]
    repositories_seen = len(candidates)

    stalls = []
    advisories = []
    skipped = []
    errors = []
    checked = 0
    for candidate in candidates:
        full_name = candidate["full_name"]
        if full_name.lower() in ignored:
            skipped.append({"repository": full_name, "reason": "ignored by configuration"})
            continue
        fork = candidate["fork"]
        if fork is None:
            try:
                detail = client.request("GET", f"/repos/{full_name}")
            except GitHubError as error:
                errors.append({"repository": full_name, "reason": f"unreadable repository: {error}"})
                continue
            fork = bool(detail.get("fork"))
        try:
            workflows = client.paginate_collection(
                f"/repos/{full_name}/actions/workflows", "workflows", {"per_page": 100})
        except GitHubError as error:
            errors.append({"repository": full_name, "reason": f"unreadable workflows: {error}"})
            continue
        try:
            runs = client.paginate_collection(
                f"/repos/{full_name}/actions/runs", "workflow_runs",
                {"status": "action_required", "created": f">={cutoff}", "per_page": 100})
        except GitHubError as error:
            errors.append({"repository": full_name, "reason": f"unreadable runs: {error}"})
            continue
        checked += 1

        disabled = [
            {"name": workflow.get("name") or workflow.get("path"),
             "path": workflow.get("path"),
             "state": workflow.get("state")}
            for workflow in workflows
            if workflow.get("state") and workflow.get("state") != ACTIVE_STATE
        ]
        unapproved = []
        for run in runs:
            age = hours_between(parse_timestamp(run.get("created_at")), now)
            if age is None or age < min_approval_age_hours:
                continue
            unapproved.append({
                "workflow": run.get("name"),
                "number": run.get("run_number"),
                "branch": run.get("head_branch"),
                "age_hours": age,
            })

        entry = {
            "repository": full_name,
            "fork": fork,
            "workflows": len(workflows),
            "disabled_workflows": disabled,
            "unapproved_runs": unapproved,
        }
        reasons = evaluate_repository(entry)
        if not reasons:
            continue
        record = dict(entry, reasons=reasons)
        if fork:
            advisories.append(record)
        else:
            stalls.append(record)

    stalls.sort(key=lambda entry: entry["repository"])
    advisories.sort(key=lambda entry: entry["repository"])
    return {
        "checked_at": now.isoformat().replace("+00:00", "Z"),
        "owner": owner,
        "lookback_days": lookback_days,
        "min_approval_age_hours": min_approval_age_hours,
        "repositories_seen": repositories_seen,
        "repositories_checked": checked,
        "stalled": bool(stalls),
        "stalls": stalls,
        "advisories": advisories,
        "skipped": skipped,
        "errors": errors,
    }


def summarize(result):
    lines = [
        f"{result['checked_at']}: checked {result['repositories_checked']} repository(ies) of "
        f"{result['repositories_seen']} for {result['owner']}; {len(result['stalls'])} stopped, "
        f"{len(result['advisories'])} fork advisory(ies), {len(result['skipped'])} skipped, "
        f"{len(result['errors'])} unreadable"
    ]
    for entry in result["stalls"]:
        for reason in entry["reasons"]:
            lines.append(f"STOPPED {entry['repository']}: {reason['kind']} - {reason['detail']}")
    for entry in result["errors"]:
        lines.append(f"UNREADABLE {entry['repository']}: {entry['reason']}")
    return "\n".join(lines)


def render_markdown(result):
    out = [
        "# Automation freshness",
        "",
        f"Checked `{result['checked_at']}`: {result['repositories_checked']} repository(ies) out of "
        f"{result['repositories_seen']} owned by `{result['owner']}`. Runs awaiting approval created in "
        f"the last {result['lookback_days']} days count once they have waited "
        f"{format_hours(result['min_approval_age_hours'])}.",
        "",
    ]
    if result["stalls"]:
        out += ["## Stopped automation", "",
                "| Repository | Signal | Detail |", "| --- | --- | --- |"]
        for entry in result["stalls"]:
            for reason in entry["reasons"]:
                out.append(f"| `{entry['repository']}` | `{reason['kind']}` | {reason['detail']} |")
        out.append("")
    else:
        out += ["## Stopped automation", "",
                "None. Every enumerated workflow is active and no run is waiting for approval.", ""]
    if result["advisories"]:
        out += ["<details><summary>Fork mirrors (incidental)</summary>", ""]
        for entry in result["advisories"]:
            for reason in entry["reasons"]:
                out.append(f"- `{entry['repository']}`: `{reason['kind']}` - {reason['detail']}")
        out += ["", "</details>", ""]
    if result["errors"]:
        out += ["## Unreadable repositories", ""]
        out += [f"- `{entry['repository']}`: {entry['reason']}" for entry in result["errors"]]
        out.append("")
    if result["skipped"]:
        out += ["<details><summary>Skipped repositories</summary>", ""]
        out += [f"- `{entry['repository']}`: {entry['reason']}" for entry in result["skipped"]]
        out += ["", "</details>", ""]
    return "\n".join(out)


def split_csv(values):
    names = []
    for value in values or []:
        names.extend(part.strip() for part in value.split(",") if part.strip())
    return names


def exit_code(result):
    """1 when an owned repository has a stall, or any repository was unreadable."""
    if result["stalled"] or result["errors"]:
        return 1
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--owner", default=None,
                        help=f"account that owns the repositories (default: the GITHUB_REPOSITORY owner, else {DEFAULT_OWNER})")
    parser.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME",
                        help="audit only this repository; repeatable (skips enumeration)")
    parser.add_argument("--ignore", action="append", default=[], metavar="OWNER/NAME",
                        help="repository to skip; repeatable or comma separated")
    parser.add_argument("--lookback-days", type=float, default=DEFAULT_LOOKBACK_DAYS,
                        help="how far back to look for runs awaiting approval")
    parser.add_argument("--min-approval-age-hours", type=float, default=DEFAULT_MIN_APPROVAL_AGE_HOURS,
                        help="how long a run may wait for approval before it counts as stalled")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--report", metavar="PATH", help="write the markdown report to PATH")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    slug = os.environ.get("GITHUB_REPOSITORY", "")
    owner = args.owner or (slug.split("/")[0] if "/" in slug else None) or DEFAULT_OWNER

    client = GitHub(token=token)
    result = audit(
        client,
        owner,
        repositories=split_csv(args.repo),
        ignore=split_csv(args.ignore),
        lookback_days=args.lookback_days,
        min_approval_age_hours=args.min_approval_age_hours,
    )

    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(result) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(summarize(result))

    code = exit_code(result)
    if code:
        print(f"automation freshness: {len(result['stalls'])} stopped, {len(result['errors'])} unreadable",
              file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
