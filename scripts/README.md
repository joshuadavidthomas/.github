# scripts

## mirror-freshness

`mirror_freshness.py` reports fork mirrors whose default branch has stopped
tracking upstream. Every mirror here is kept level by one external bot
(`pull[bot]`), and nothing watched that bot, so a fork that diverges — normally
because a workflow inherited from upstream commits to the mirror's own default
branch — stayed stale silently. `joshuadavidthomas/hiwave` sat conflicted for
eight months.

It reads public repository data only and needs no credential beyond the token
the workflow already has. Run it locally with the `gh` CLI's token:

```sh
GH_TOKEN=$(gh auth token) python3 scripts/mirror_freshness.py --owner joshuadavidthomas
```

Useful flags: `--repo OWNER/NAME` (audit one fork), `--ignore OWNER/NAME`
(a working fork that is not a mirror), `--json`, `--report PATH`,
`--max-pull-request-age-hours`, `--max-lag-hours`.

Exit status is 1 when a mirror is stalled or unverifiable, so a green run means
every mirror was read and is level with upstream. The scheduled workflow
`.github/workflows/mirror-freshness.yml` runs it four times a day, writes
`reports/mirror-freshness.md`, and keeps one tracking issue up to date.

Tests (no network, no dependencies):

```sh
python3 scripts/test_mirror_freshness.py
```
