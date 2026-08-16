# traffic

Public home for [nagusubra](https://github.com/nagusubra) repository traffic
dashboards and raw data.

- **Hub:** https://nagusubra.github.io/traffic/ (tabs that link to each repo's dashboard)
- **Content:** one dashboard per repo under `doc/metric/<repo>/`, raw CSVs under
  `.metrics/data/<repo>/`.

## Dashboards

| Repo | Dashboard |
|------|-----------|
| timeseries-qc | https://nagusubra.github.io/traffic/doc/metric/timeseries-qc/ |
| industry-hackathon-lab | https://nagusubra.github.io/traffic/doc/metric/industry-hackathon-lab/ |

Add a new repo by:

1. Creating `doc/metric/<repo>/index.html` (copy an existing dashboard and change
   its `SLUG`/`REPO`/`DESC` constants) and `doc/metric/<repo>/badge.svg`,
2. Adding the repo to `.metrics/data/<repo>/`,
3. Appending one entry to the `DASHBOARDS` array in the root `index.html`,
4. Adding `nagusubra/<repo>` to the loop in `.github/workflows/archive.yml`.

## How it works

`.github/workflows/archive.yml` runs hourly. For each source repo it runs
`.github/scripts/archive_traffic.py` with `GITHUB_REPOSITORY=nagusubra/<repo>` and
a fine-grained PAT (secret `GH_TRAFFIC_TOKEN`, traffic read access on the source
repos). The archiver merges the 14-day views/clones window, snapshots
referrers/paths and repo counters, rebuilds star/fork/issue history, regenerates
`doc/metric/<repo>/badge.svg`, and commits to `main`. The source repos stay free
of any collector code or data.

Data layout per repo (`.metrics/data/<repo>/`):

| File | Contents |
|------|----------|
| `views.csv` / `clones.csv` | daily totals + uniques (14-day window, newer wins) |
| `referrers.csv` / `paths.csv` | daily top-referrer and top-path snapshots |
| `repo.csv` | daily counters (stars, forks, watchers, subscribers, open issues, contributors, releases) |
| `stars.csv` / `forks.csv` | cumulative history (backfilled to repo creation) |
| `commits.csv` | weekly commit totals |
| `issues.csv` | daily issues/PRs opened, closed, merged |

GitHub restricts `stargazers` for some token scopes, so `stars.csv` may be
frozen at the last successfully archived value.