#!/usr/bin/env python3
"""Report fork mirrors that have stopped syncing with their upstream parent.

Every mirrored upstream project here is tracked as a fork, and one external bot
(``pull[bot]``) keeps the fork level by opening a sync pull request whenever the
parent's default branch moves. Nothing watches that bot: a fork that diverges
(usually because a workflow inherited from upstream commits to the mirror's own
default branch) wedges its sync pull request on the same conflict forever, and
the fork silently serves a stale revision of upstream.

This script reports those stalls. It reads public repository data only, so it
needs no credential beyond the workflow's own token.

A mirror is reported as stalled when any of these holds:

* ``diverged`` -- the fork's default branch has commits upstream does not have
  while upstream also has commits the fork lacks. That branch pair can never
  fast-forward, so every sync attempt lands on the same conflict.
* ``stale_sync_pull_request`` -- an open sync pull request opened by ``pull[bot]``
  has been waiting longer than ``--max-pull-request-age-hours``. Mirrors that are
  syncing normally merge seconds after the pull request opens.
* ``no_recent_sync`` -- the fork is strictly behind upstream and has not been
  pushed to for longer than ``--max-lag-hours``. Upstream moved and nothing has
  synced since, with no sync pull request open to explain it.

Exit status is 1 when at least one mirror is stalled, or when any mirror could
not be verified, and 0 otherwise: a green run means every mirror was read and is
level with upstream.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_ROOT = "https://api.github.com"
DEFAULT_OWNER = "joshuadavidthomas"
SYNC_PULL_REQUEST_AUTHOR = "pull[bot]"
SYNC_PULL_REQUEST_TITLE_PREFIX = "[pull]"
TRACKING_ISSUE_TITLE = "Mirror sync stalls detected"
DEFAULT_MAX_PULL_REQUEST_AGE_HOURS = 6.0
DEFAULT_MAX_LAG_HOURS = 24.0


class GitHubError(RuntimeError):
    """A GitHub API call that did not return a usable response."""


class GitHub:
    """Minimal read/write client for the handful of endpoints this check needs."""

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
        request.add_header("User-Agent", "mirror-freshness")
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
        params = dict(params or {})
        per_page = int(params.get("per_page", 30))
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


def is_sync_pull_request(pull):
    """True for the upstream-sync pull requests the mirror bot opens."""
    author = ((pull.get("user") or {}).get("login") or "").lower()
    title = pull.get("title") or ""
    return author == SYNC_PULL_REQUEST_AUTHOR or title.startswith(SYNC_PULL_REQUEST_TITLE_PREFIX)


def oldest_open_sync_pull_request(pulls, now):
    """Return (age_hours, number) of the longest-open sync pull request, or None."""
    oldest = None
    for pull in pulls:
        if pull.get("state") not in (None, "open"):
            continue
        if not is_sync_pull_request(pull):
            continue
        age = hours_between(parse_timestamp(pull.get("created_at")), now)
        if age is None:
            continue
        if oldest is None or age > oldest[0]:
            oldest = (age, pull.get("number"))
    return oldest


def non_mirror_reason(detail):
    """Why this fork is not a pure mirror of its upstream, or None if it is one."""
    parent = detail.get("parent") or {}
    if not parent.get("full_name"):
        return "not a fork (no upstream parent)"
    branch = detail.get("default_branch")
    parent_branch = parent.get("default_branch")
    if branch != parent_branch:
        return f"default branch {branch!r} differs from upstream {parent_branch!r}"
    return None


def evaluate_mirror(mirror, max_pull_request_age_hours, max_lag_hours):
    """Return the stall reasons for one mirror. Pure function."""
    reasons = []
    ahead = mirror["ahead_by"]
    behind = mirror["behind_by"]

    if ahead > 0 and behind > 0:
        reasons.append({
            "kind": "diverged",
            "detail": (
                f"fork is {ahead} commit(s) ahead of upstream, so the upstream sync can "
                f"never fast-forward ({behind} upstream commit(s) still pending)"
            ),
        })

    pull_request = mirror.get("sync_pull_request")
    if pull_request and pull_request["age_hours"] > max_pull_request_age_hours:
        reasons.append({
            "kind": "stale_sync_pull_request",
            "detail": (
                f"sync pull request #{pull_request['number']} has been open "
                f"{format_hours(pull_request['age_hours'])}"
            ),
        })

    lag = mirror.get("lag_hours")
    if behind > 0 and ahead == 0 and lag is not None and lag > max_lag_hours:
        reasons.append({
            "kind": "no_recent_sync",
            "detail": (
                f"fork is {behind} commit(s) behind upstream and has not been pushed to "
                f"for {format_hours(lag)}"
            ),
        })

    return reasons


def audit(client, owner, repositories=None, ignore=(), max_pull_request_age_hours=DEFAULT_MAX_PULL_REQUEST_AGE_HOURS,
          max_lag_hours=DEFAULT_MAX_LAG_HOURS, now=None):
    """Audit the owner's fork mirrors and return a JSON-serialisable result."""
    now = now or datetime.now(timezone.utc)
    ignored = {name.lower() for name in ignore}

    if repositories:
        candidates = [{"full_name": name} for name in repositories]
        forks_seen = len(candidates)
    else:
        forks = [repo for repo in client.paginate(f"/users/{owner}/repos", {"type": "owner", "per_page": 100})
                 if repo.get("fork")]
        candidates = [{"full_name": repo.get("full_name") or f"{owner}/{repo.get('name')}"} for repo in forks]
        forks_seen = len(candidates)

    mirrors = []
    skipped = []
    errors = []
    for candidate in candidates:
        full_name = candidate["full_name"]
        if full_name.lower() in ignored:
            skipped.append({"repository": full_name, "reason": "ignored by configuration"})
            continue
        try:
            detail = client.request("GET", f"/repos/{full_name}")
        except GitHubError as error:
            errors.append({"repository": full_name, "reason": f"unreadable: {error}"})
            continue
        reason = non_mirror_reason(detail)
        if reason:
            skipped.append({"repository": full_name, "reason": reason})
            continue
        parent = detail["parent"]
        mirror = {
            "repository": full_name,
            "branch": detail["default_branch"],
            "upstream": parent["full_name"],
            "pushed_at": detail.get("pushed_at"),
            "lag_hours": hours_between(parse_timestamp(detail.get("pushed_at")), now),
            "status": None,
            "ahead_by": 0,
            "behind_by": 0,
            "sync_pull_request": None,
            "error": None,
        }
        try:
            upstream_owner = parent["full_name"].split("/")[0]
            compare = client.request(
                "GET",
                f"/repos/{full_name}/compare/{upstream_owner}:{parent['default_branch']}...{owner}:{mirror['branch']}",
            )
            mirror["status"] = compare.get("status")
            mirror["ahead_by"] = int(compare.get("ahead_by") or 0)
            mirror["behind_by"] = int(compare.get("behind_by") or 0)
        except GitHubError as error:
            mirror["error"] = str(error)
        mirrors.append(mirror)

    for mirror in mirrors:
        if mirror["error"] or (mirror["ahead_by"] == 0 and mirror["behind_by"] == 0):
            continue
        try:
            pulls = client.paginate(f"/repos/{mirror['repository']}/pulls", {"state": "open", "per_page": 100})
        except GitHubError as error:
            mirror["error"] = str(error)
            continue
        oldest = oldest_open_sync_pull_request(pulls, now)
        if oldest:
            mirror["sync_pull_request"] = {"number": oldest[1], "age_hours": oldest[0]}

    stalls = []
    advisories = []
    for mirror in mirrors:
        if mirror["error"]:
            errors.append({"repository": mirror["repository"], "reason": mirror["error"]})
            continue
        reasons = evaluate_mirror(mirror, max_pull_request_age_hours, max_lag_hours)
        if reasons:
            stalls.append(dict(mirror, reasons=reasons))
        elif mirror["ahead_by"] > 0:
            advisories.append({
                "repository": mirror["repository"],
                "ahead_by": mirror["ahead_by"],
                "detail": (
                    f"fork carries {mirror['ahead_by']} commit(s) upstream does not have, but "
                    "upstream has nothing pending, so no sync is due"
                ),
            })

    stalls.sort(key=lambda entry: (-entry["behind_by"], entry["repository"]))
    return {
        "checked_at": now.isoformat().replace("+00:00", "Z"),
        "owner": owner,
        "forks_seen": forks_seen,
        "mirrors_checked": len(mirrors),
        "stalled": bool(stalls),
        "stalls": stalls,
        "advisories": advisories,
        "skipped": skipped,
        "errors": errors,
    }


def summarize(result):
    lines = [
        f"{result['checked_at']}: checked {result['mirrors_checked']} mirror(s) of "
        f"{result['forks_seen']} fork(s) for {result['owner']}; "
        f"{len(result['stalls'])} stalled, {len(result['advisories'])} ahead with nothing pending, "
        f"{len(result['skipped'])} skipped, {len(result['errors'])} unreadable"
    ]
    for entry in result["stalls"]:
        pulls = entry["sync_pull_request"]
        pull_note = f" PR#{pulls['number']} {format_hours(pulls['age_hours'])}" if pulls else ""
        reasons = "; ".join(f"{reason['kind']} - {reason['detail']}" for reason in entry["reasons"])
        lines.append(f"STALLED {entry['repository']} ({entry['branch']} <- {entry['upstream']}){pull_note}: {reasons}")
    for entry in result["errors"]:
        lines.append(f"UNREADABLE {entry['repository']}: {entry['reason']}")
    return "\n".join(lines)


def render_markdown(result):
    out = ["# Mirror freshness", "",
           f"Checked `{result['checked_at']}`: {result['mirrors_checked']} mirror(s) out of "
           f"{result['forks_seen']} fork(s) owned by `{result['owner']}`.", ""]
    if result["stalls"]:
        out += ["## Stalled mirrors", "",
                "| Mirror | Upstream | Ahead | Behind | Sync PR | Reason |",
                "| --- | --- | --- | --- | --- | --- |"]
        for entry in result["stalls"]:
            pulls = entry["sync_pull_request"]
            pull_cell = f"#{pulls['number']} ({format_hours(pulls['age_hours'])})" if pulls else "none"
            reason_cell = "<br>".join(f"`{reason['kind']}`: {reason['detail']}" for reason in entry["reasons"])
            out.append(f"| `{entry['repository']}` | `{entry['upstream']}` | {entry['ahead_by']} | "
                       f"{entry['behind_by']} | {pull_cell} | {reason_cell} |")
        out.append("")
    else:
        out += ["## Stalled mirrors", "", "None. Every mirror's default branch is level with upstream.", ""]
    if result["advisories"]:
        out += ["## Ahead of upstream, nothing pending", ""]
        out += [f"- `{entry['repository']}`: {entry['detail']}" for entry in result["advisories"]]
        out.append("")
    if result["errors"]:
        out += ["## Unreadable mirrors", ""]
        out += [f"- `{entry['repository']}`: {entry['reason']}" for entry in result["errors"]]
        out.append("")
    if result["skipped"]:
        out += ["<details><summary>Skipped forks (not pure mirrors)</summary>", ""]
        out += [f"- `{entry['repository']}`: {entry['reason']}" for entry in result["skipped"]]
        out += ["", "</details>", ""]
    return "\n".join(out)


def build_issue_body(result):
    return "\n".join([
        render_markdown(result),
        "---",
        "",
        "A mirror whose default branch carries its own commits can never be synced again: "
        "the upstream sync pull request collides with those commits on every attempt, and the "
        "fork quietly serves a stale revision. Two ways out, per mirror: reset the fork's default "
        "branch to upstream (discarding the local commits), or stop the fork-local workflow that "
        "commits to it (and archive the fork if it is not meant to be a mirror).",
        "",
        "This issue is updated in place by the scheduled mirror-freshness workflow and closed "
        "automatically once every mirror is level again.",
    ])


def sync_tracking_issue(client, repository, result):
    """Create, update, or close the single tracking issue for this audit."""
    existing = None
    for issue in client.paginate(f"/repos/{repository}/issues", {"state": "open", "per_page": 100}):
        if "pull_request" in issue:
            continue
        if issue.get("title") == TRACKING_ISSUE_TITLE:
            existing = issue
            break
    if result["stalled"]:
        body = build_issue_body(result)
        if existing:
            client.request("PATCH", f"/repos/{repository}/issues/{existing['number']}", body={"body": body})
            return {"action": "updated", "number": existing["number"]}
        created = client.request("POST", f"/repos/{repository}/issues",
                                 body={"title": TRACKING_ISSUE_TITLE, "body": body})
        return {"action": "opened", "number": created["number"]}
    if existing:
        client.request("POST", f"/repos/{repository}/issues/{existing['number']}/comments",
                       body={"body": f"All mirrors are level with upstream as of {result['checked_at']}."})
        client.request("PATCH", f"/repos/{repository}/issues/{existing['number']}", body={"state": "closed"})
        return {"action": "closed", "number": existing["number"]}
    return {"action": "none", "number": None}


def split_csv(values):
    names = []
    for value in values or []:
        names.extend(part.strip() for part in value.split(",") if part.strip())
    return names


def exit_code(result):
    """1 when a mirror is stalled or any mirror could not be verified, else 0."""
    if result["stalled"] or result["errors"]:
        return 1
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--owner", default=None,
                        help=f"account that owns the forks (default: the GITHUB_REPOSITORY owner, else {DEFAULT_OWNER})")
    parser.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME",
                        help="audit only this fork; repeatable (skips enumeration)")
    parser.add_argument("--ignore", action="append", default=[], metavar="OWNER/NAME",
                        help="fork to skip; repeatable or comma separated")
    parser.add_argument("--max-pull-request-age-hours", type=float, default=DEFAULT_MAX_PULL_REQUEST_AGE_HOURS,
                        help="age at which an open sync pull request counts as stalled")
    parser.add_argument("--max-lag-hours", type=float, default=DEFAULT_MAX_LAG_HOURS,
                        help="how long a fork may sit strictly behind upstream before it counts as stalled")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--report", metavar="PATH", help="write the markdown report to PATH")
    parser.add_argument("--sync-issue", action="store_true",
                        help="open, update, or close the tracking issue in --issue-repository")
    parser.add_argument("--issue-repository", default=None, metavar="OWNER/NAME",
                        help="repository holding the tracking issue (default: GITHUB_REPOSITORY)")
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
        max_pull_request_age_hours=args.max_pull_request_age_hours,
        max_lag_hours=args.max_lag_hours,
    )

    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(result) + "\n", encoding="utf-8")

    if args.sync_issue:
        repository = args.issue_repository or slug
        if not repository:
            print("--sync-issue needs --issue-repository or GITHUB_REPOSITORY", file=sys.stderr)
            return 1
        outcome = sync_tracking_issue(client, repository, result)
        result["tracking_issue"] = dict(outcome, repository=repository)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(summarize(result))

    code = exit_code(result)
    if code:
        print(f"mirror freshness: {len(result['stalls'])} stalled, {len(result['errors'])} unverifiable",
              file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
