#!/usr/bin/env python3
"""Archive GitHub repository traffic into .metrics/data/*.csv and refresh
doc/metric/*/badge.svg for the nagusubra/traffic dashboard repo.

GitHub only exposes the last 14 days of traffic data, so this is meant to run
on a schedule (see .github/workflows/archive.yml). Each run:

  * fetches the full 14-day views/clones window and merges it into the
    persistent CSVs (newer values win),
  * snapshots the top referrers/paths dated with the traffic as-of date
    (the most recent day in the views window, so all datasets share one
    timeline and the dashboard's range filters apply uniformly),
  * snapshots repo-level counters (stars, forks, true watchers/subscribers,
    open issues, contributors, releases),
  * rebuilds full star/fork history (backfillable to repo creation via the
    stargazers/forks endpoints) and weekly commit activity,
  * rebuilds issue/PR open/close/merge history,
  * regenerates an all-time total-views badge.

Data and badge land in per-repo subdirectories keyed by the source repo's name
(e.g. .metrics/data/timeseries-qc/ and doc/metric/timeseries-qc/), so a single
run can archive any number of repos into one shared repo.

Environment:
    GH_TOKEN            GitHub token with traffic read access (fine-grained PAT
                        stored as GH_TRAFFIC_TOKEN Actions secret). Required.
    GITHUB_REPOSITORY   "owner/repo" of the SOURCE repo to archive.

Stdlib only.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone

API = "https://api.github.com"
DEFAULT_REPO = "nagusubra/timeseries-qc"

HERE = os.path.dirname(os.path.abspath(__file__))          # <root>/.github/scripts
ROOT = os.path.dirname(os.path.dirname(HERE))              # <root>

TOKEN = os.environ.get("GH_TOKEN", "").strip()
REPO = os.environ.get("GITHUB_REPOSITORY", "").strip() or DEFAULT_REPO
SLUG = REPO.split("/")[-1]

DATA_DIR = os.path.join(ROOT, ".metrics", "data", SLUG)
DOC_DIR = os.path.join(ROOT, "doc", "metric", SLUG)

VIEWS_FIELDS = ["date", "views", "uniques"]
CLONES_FIELDS = ["date", "clones", "uniques"]
REFERRERS_FIELDS = ["date", "referrer", "count", "uniques"]
PATHS_FIELDS = ["date", "path", "title", "count", "uniques"]
REPO_FIELDS = ["date", "stars", "forks", "watchers", "subscribers", "open_issues", "contributors", "releases"]
STARS_FIELDS = ["date", "stars"]
FORKS_FIELDS = ["date", "forks"]
COMMITS_FIELDS = ["week_start", "total"]
ISSUES_FIELDS = ["date", "issues_opened", "issues_closed", "prs_opened", "prs_closed", "prs_merged"]

BADGE_COLOR = "#475569"


def log(*args) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}]", *args, flush=True)


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def api_get(path: str, accept: str = "application/vnd.github+json"):
    req = urllib.request.Request(API + path, method="GET")
    req.add_header("Accept", accept)
    req.add_header("User-Agent", "repo-traffic-archiver")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body.strip() else None
    except urllib.error.HTTPError as e:
        log(f"[warn] GET {path} -> HTTP {e.code} ({e.reason})")
        return None
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        log(f"[warn] GET {path} -> {e}")
        return None


def api_get_paged(path: str, per_page: int = 100, max_pages: int = 20, accept: str = "application/vnd.github+json") -> list:
    out: list = []
    for page in range(1, max_pages + 1):
        sep = "&" if "?" in path else "?"
        data = api_get(f"{path}{sep}per_page={per_page}&page={page}", accept=accept)
        if not isinstance(data, list):
            break
        out.extend(data)
        if len(data) < per_page:
            break
    return out


def append_rows(path: str, fields: list[str], keys: list[str], new_rows: list[dict]) -> None:
    """Append new rows to a CSV without EVER modifying or deleting existing rows.

    Guarantees:
      * Existing rows (matched by ``keys``) are never touched.
      * The file is opened in append mode, so it is never truncated or
        re-written; only brand-new rows are appended at the end.
      * If a row's key already exists, the new value is discarded -- the stored
        value always wins, so no historical data can ever be overwritten or lost.
    """
    existing: set[tuple[str, ...]] = set()
    has_header = False
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, newline="", encoding="utf-8") as f:
            for i, row in enumerate(csv.reader(f)):
                if not row:
                    continue
                if i == 0:
                    has_header = True
                    continue
                key = tuple(c.strip() for c in row[: len(keys)])
                if all(k not in (None, "") for k in key):
                    existing.add(key)

    to_add: list[dict] = []
    seen = set(existing)
    for row in new_rows:
        key = tuple(str(row.get(k, "")).strip() for k in keys)
        if key in seen:
            continue
        seen.add(key)
        to_add.append({k: str(row.get(k, "")) for k in fields})

    if not to_add:
        log(f"{path} already current ({len(existing)} rows); nothing new to append")
        return

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not has_header:
            writer.writeheader()
        writer.writerows(to_add)
    log(f"appended {len(to_add)} new row(s) to {path} ({len(existing) + len(to_add)} total rows)")


def read_all(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r]


def cumulative_series(created: str, event_days: list[str]) -> list[dict]:
    """Daily cumulative counts from created-date to today."""
    start = date.fromisoformat(created)
    end = today_utc()
    counts = Counter(d[:10] for d in event_days if d)
    series: list[dict] = []
    running = 0
    cur = start
    while cur <= end:
        running += counts.get(cur.isoformat(), 0)
        series.append({"date": cur.isoformat(), "value": running})
        cur += timedelta(days=1)
    return series


def archive_views() -> tuple[str, bool]:
    """Merge the 14-day views window; return (as-of date, fetch succeeded)."""
    data = api_get(f"/repos/{REPO}/traffic/views?per=day")
    ok = isinstance(data, dict) and isinstance(data.get("views"), list)
    data = data or {}
    rows = [
        {"date": d["timestamp"][:10], "views": d["count"], "uniques": d["uniques"]}
        for d in data.get("views", [])
    ]
    append_rows(os.path.join(DATA_DIR, "views.csv"), VIEWS_FIELDS, ["date"], rows)
    dates = [d["timestamp"][:10] for d in data.get("views", [])]
    return (max(dates) if dates else today_utc()), ok


def archive_clones() -> bool:
    """Merge the 14-day clones window; return True if the fetch succeeded."""
    data = api_get(f"/repos/{REPO}/traffic/clones?per=day")
    ok = isinstance(data, dict) and isinstance(data.get("clones"), list)
    data = data or {}
    rows = [
        {"date": d["timestamp"][:10], "clones": d["count"], "uniques": d["uniques"]}
        for d in data.get("clones", [])
    ]
    append_rows(os.path.join(DATA_DIR, "clones.csv"), CLONES_FIELDS, ["date"], rows)
    return ok


def archive_referrers(asof: str) -> None:
    rows = [
        {"date": asof, "referrer": r.get("referrer", ""), "count": r.get("count", 0), "uniques": r.get("uniques", 0)}
        for r in (api_get(f"/repos/{REPO}/traffic/popular/referrers") or [])
    ]
    append_rows(os.path.join(DATA_DIR, "referrers.csv"), REFERRERS_FIELDS, ["date", "referrer"], rows)


def archive_paths(asof: str) -> None:
    rows = [
        {
            "date": asof,
            "path": p.get("path", ""),
            "title": p.get("title", ""),
            "count": p.get("count", 0),
            "uniques": p.get("uniques", 0),
        }
        for p in (api_get(f"/repos/{REPO}/traffic/popular/paths") or [])
    ]
    append_rows(os.path.join(DATA_DIR, "paths.csv"), PATHS_FIELDS, ["date", "path"], rows)


def archive_repo() -> None:
    data = api_get(f"/repos/{REPO}")
    if not data:
        return
    today = today_utc().isoformat()
    contributors = len(api_get_paged(f"/repos/{REPO}/contributors"))
    releases = len(api_get_paged(f"/repos/{REPO}/releases"))
    rows = [
        {
            "date": today,
            "stars": data.get("stargazers_count", 0),
            "forks": data.get("forks_count", 0),
            "watchers": data.get("watchers_count", 0),
            "subscribers": data.get("subscribers_count", 0),
            "open_issues": data.get("open_issues_count", 0),
            "contributors": contributors,
            "releases": releases,
        }
    ]
    append_rows(os.path.join(DATA_DIR, "repo.csv"), REPO_FIELDS, ["date"], rows)
    created = (data.get("created_at") or "").split("T")[0]
    if created:
        archive_star_history(created)
        archive_fork_history(created)


def archive_star_history(created: str) -> None:
    stars = api_get_paged(f"/repos/{REPO}/stargazers", accept="application/vnd.github.star+json")
    if not stars:
        # e.g. 403 from a token that cannot list stargazers (GitHub restricts the
        # endpoint to admins/collaborators). Never clobber existing history.
        log("[warn] stargazers fetch empty/failed; keeping existing stars.csv")
        return
    rows = [{"date": r["date"], "stars": r["value"]} for r in cumulative_series(created, [s.get("starred_at", "") for s in stars])]
    append_rows(os.path.join(DATA_DIR, "stars.csv"), STARS_FIELDS, ["date"], rows)


def archive_fork_history(created: str) -> None:
    forks = api_get_paged(f"/repos/{REPO}/forks?sort=oldest")
    if not forks:
        log("[warn] forks fetch empty/failed; keeping existing forks.csv")
        return
    rows = [{"date": r["date"], "forks": r["value"]} for r in cumulative_series(created, [f.get("created_at", "") for f in forks])]
    append_rows(os.path.join(DATA_DIR, "forks.csv"), FORKS_FIELDS, ["date"], rows)


def archive_commits() -> None:
    data = api_get(f"/repos/{REPO}/stats/commit_activity")
    if not isinstance(data, list):
        log("[warn] commit_activity not ready (GitHub caches it); skipping")
        return
    rows = [
        {"week_start": datetime.fromtimestamp(int(w["week"]), tz=timezone.utc).date().isoformat(), "total": w.get("total", 0)}
        for w in data if isinstance(w, dict) and w.get("total")
    ]
    append_rows(os.path.join(DATA_DIR, "commits.csv"), COMMITS_FIELDS, ["week_start"], rows)


def archive_issues() -> None:
    issues = api_get_paged(f"/repos/{REPO}/issues?state=all")
    pulls = api_get_paged(f"/repos/{REPO}/pulls?state=all")
    merged_by_day = Counter()
    for p in pulls:
        merged = (p.get("merged_at") or "")[:10]
        if merged:
            merged_by_day[merged] += 1

    opened: Counter = Counter()
    closed: Counter = Counter()
    pr_opened: Counter = Counter()
    pr_closed: Counter = Counter()
    for i in issues:
        created = (i.get("created_at") or "")[:10]
        closed_at = (i.get("closed_at") or "")[:10]
        if i.get("pull_request"):
            if created:
                pr_opened[created] += 1
            if closed_at:
                pr_closed[closed_at] += 1
        else:
            if created:
                opened[created] += 1
            if closed_at:
                closed[closed_at] += 1

    all_days = set(opened) | set(closed) | set(pr_opened) | set(pr_closed) | set(merged_by_day)
    rows = [
        {
            "date": d,
            "issues_opened": opened.get(d, 0),
            "issues_closed": closed.get(d, 0),
            "prs_opened": pr_opened.get(d, 0),
            "prs_closed": pr_closed.get(d, 0),
            "prs_merged": merged_by_day.get(d, 0),
        }
        for d in sorted(all_days)
    ]
    append_rows(os.path.join(DATA_DIR, "issues.csv"), ISSUES_FIELDS, ["date"], rows)


def format_int(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


def render_badge(label: str, value: str, color: str = BADGE_COLOR) -> None:
    # shields.io-style badge, left label on grey, right value in color.
    font = 11
    lw = int(len(label) * 7.1 + 14)
    rw = int(len(value) * 7.6 + 14)
    W, H = lw + rw, 20
    lcx, rcx = lw // 2, lw + rw // 2
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{W}" height="{H}" role="img" aria-label="{label}: {value}">
  <title>{label}: {value}</title>
  <clipPath id="r"><rect width="{W}" height="{H}" rx="3" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="{lw}" height="{H}" fill="#555"/>
    <rect x="{lw}" width="{rw}" height="{H}" fill="{color}"/>
    <rect width="{W}" height="{H}" fill="url(#s)"/>
  </g>
  <defs><linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#fff" stop-opacity=".08"/><stop offset="1" stop-opacity=".08"/></linearGradient></defs>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="{font}">
    <text x="{lcx}" y="14">{label}</text>
    <text x="{rcx}" y="14" font-weight="bold">{value}</text>
  </g>
</svg>
"""
    os.makedirs(DOC_DIR, exist_ok=True)
    path = os.path.join(DOC_DIR, "badge.svg")
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    log(f"wrote {path}")


def refresh_badge() -> None:
    total = sum(int(r.get("views", 0) or 0) for r in read_all(os.path.join(DATA_DIR, "views.csv")))
    render_badge("total views", format_int(total))


def main() -> int:
    if not os.environ.get("CI"):
        log(f"local run | repo={REPO} | token={'present' if TOKEN else 'MISSING (traffic endpoints will be skipped)'}")
    log(f"archiving traffic for {REPO} -> slug {SLUG}")
    asof, views_ok = archive_views()
    clones_ok = archive_clones()
    archive_referrers(asof)
    archive_paths(asof)
    archive_repo()
    archive_commits()
    archive_issues()
    refresh_badge()

    failed = [name for name, ok in (("views", views_ok), ("clones", clones_ok)) if not ok]
    if failed:
        log(f"[error] {REPO}: traffic API returned an error for {', '.join(failed)}. "
            "Check that GH_TRAFFIC_TOKEN is a valid PAT with repo scope (classic) or "
            "Administration:Read (fine-grained) on the source repo.")
        return 1
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())