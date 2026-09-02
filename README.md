# deepseek-balance

A deepseek-balance project generated with genproj

## Capabilities

This project includes the following capabilities:

- **Docker**: Docker support for the project.
- **Python DevContainer**: Sets up a VS Code DevContainer with Python environment.
- **Docker Container**: Containerize the project and publish to the GitHub Container Registry (GHCR) for deployment to a NAS or self-hosted host via Docker Compose. Mutually exclusive with other deployment systems.
- **Doppler Secrets Management**: Integrates Doppler for secure secrets management. Enables the various MCP servers that rely on privileged tokens to access their services (e.g. CircleCI, GitHub, SonarQube).
- **CircleCI Integration**: Configures CircleCI for continuous integration and deployment. Requires Doppler: the CircleCI MCP server needs CircleCI tokens that are only available through Doppler.
- **Ruff (Python code quality)**: Adds fast, zero-configuration Python linting with Ruff (rules live in pyproject.toml [tool.ruff] and the CI test job runs `ruff check src tests`). Requires a Python devcontainer.

## Setup

1. Clone the repository
2. Create a virtualenv and install the package with dev extras:

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -e ".[dev]"
   ```

3. Run the checks:

   ```bash
   ruff check src tests
   pytest -v
   ```

## Homepage widget — daily heartbeat

The homepage widget (served at `/`) is a **daily heartbeat** view rather than a
raw balance trend chart. It answers three questions at a glance:

- **Spent today & is that normal?** — today's spend compared against the median
  spend of recent complete days (`NORMAL_DAYS`, default 14), so you can tell at
  a glance whether you're on track, below average, or pacing above normal.
- **Projected spend today** — today's spend pace extended to midnight, so you
  know how much the day is shaping up to cost if the current pace holds.
- **Spend-interval summary** — over the last `SPEND_SUMMARY_HOURS` (default 24)
  the widget counts how many `SPEND_SLICE_MINUTES` intervals actually had spend
  (zero-spend intervals are ignored), plus the median interval spend. It then
  flags a single interval as **unusually high** only when its spend is a robust
  statistical outlier vs. the recent interval distribution — a spike — rather
  than when it merely exceeds a tiny share of your daily budget. This separates
  the two signals that matter: *one abnormally large interval* vs. *lots of
  ordinary usage* (many intervals, none individually high), which is fine. The
  spike rule is `median + SPIKE_MULT × MAD`, and always at least
  `SPIKE_MIN_RATIO × median`. Every spent interval is bucketed into
  **unusually high** / **around normal** / **below normal** (below = less than
  `1/NORMAL_BAND` of the median interval spend). Until there are at least
  `MIN_INTERVALS_FOR_BASELINE` spent intervals in the window, the widget says
  "not enough data" instead of guessing.

The poller defaults to `POLL_INTERVAL=1m` (granular enough to notice a sudden
single-interval decline). Tune the detection without touching code via these
environment variables:

| Env var                    | Default | Meaning                                        |
| -------------------------- | ------- | ---------------------------------------------- |
| `POLL_INTERVAL`            | `1m`    | Balance polling cadence (finer = catches faster drops). |
| `MAX_GAP_MINUTES`          | `30`    | Skip comparing drops across gaps wider than this. |
| `NORMAL_DAYS`              | `14`    | Days of history used for the "typical day" baseline. |
| `SPEND_SLICE_MINUTES`      | `5`     | Width of each spend interval in the summary.   |
| `SPEND_SUMMARY_HOURS`      | `24`    | How far back the spend-interval summary looks. |
| `SPIKE_MULT`               | `3.0`   | Outlier multiple vs the interval spread (MAD). |
| `SPIKE_MIN_RATIO`          | `2.0`   | A spike must always be at least this × median. |
| `MIN_INTERVALS_FOR_BASELINE` | `10`  | Spent intervals needed before judging spikes.  |
| `NORMAL_BAND`              | `2.0`   | "Below normal" = less than 1/this × median.    |

> **Timezones:** the widget asks `/balance/daily` with the browser's local time
> (including its UTC offset), so "today", the heartbeat day, and the summary
> all align with the viewer's local day — not server UTC.

The data endpoint is `/balance/daily` (accepts an optional `now` query param
carrying the client's local time so "today" matches the viewer's timezone).

### Usage time, history & the three daily charts

The **Spend — today** summary also reports **Usage time** — a rough proxy for
how long the API was actively worked, defined as the number of spend intervals
with spend × the interval width (e.g. `SPEND_SLICE_MINUTES`).

Everything now lives on the **single homepage page** (`/`; `/history` is a
back-compat alias). Under today's heartbeat and interval summary, three
separate charts show the previous days (each range has its own scale, so no
dual-axis juggling):

1. **Spend per day** — total spend per complete day.
2. **Usage time per day** — minutes of active use (spent slices × width).
3. **Cost per hour** — spend ÷ active hours, shown in the **minor unit**
   (cents / fen, i.e. ×100) per hour for readability, since per-minute figures
   are tiny.

Today appears as the **rightmost dashed bar** on each chart — spend uses the
**projected** full-day amount, usage uses the partial actual so far, so an
estimate is never confused with history. A range toggle switches between the
last **14 days** (default), **last month**, and **all time**. Ranges are
**clipped to where data actually exists**, so no blank bars pad the start
before you began collecting. A summary line shows the **average** spend/day,
usage/day and cost/hour across the complete days in view (today's estimate is
excluded from those averages). Charts are fully responsive with no horizontal
scroll, so they work on a phone.

The data endpoint is `/balance/days` (`days` = number of complete days, or `0`
for all time; `now` optional for local day alignment).

## Analysis API & MCP

The per-interval classification is exposed two ways so you can dig into *why*
a period was cheap or expensive (e.g. correlating a "high" window with what
was running).

### REST API

`GET /spend/intervals` returns every spent interval over the last `hours`
(default 24), each with its start time, spend, and a `bucket` classification
(`high` / `normal` / `below`), plus the thresholds used. Narrow to one
classification with `bucket=high|normal|below`:

```bash
# All spent intervals, last 24h, 5-minute slices
curl "http://localhost:3000/spend/intervals?hours=24&slice_minutes=5"

# Only the unusually-high periods, ready for a deeper dive
curl "http://localhost:3000/spend/intervals?hours=24&slice_minutes=5&bucket=high"
```

### MCP server

An MCP server (`deepseek_balance.mcp_server`) lets an agent query the same
data as tools. Run it over **stdio** (for local agents):

```bash
python -m deepseek_balance.mcp_server          # or: deepseek-balance-mcp
```

or over **Streamable HTTP** (for remote agents). The `mcp` service in
`docker-compose.yml` exposes it on `:3100` (endpoint `http://<host>:3100/mcp`).

Tools exposed:

| Tool                    | Purpose                                                        |
| ----------------------- | -------------------------------------------------------------- |
| `spend_summary`         | Aggregate counts of high / normal / below intervals + thresholds. |
| `list_spend_intervals`  | Individual intervals, optionally filtered by `bucket`.         |
| `get_balance_latest`    | Most recent balance snapshot.                                  |
| `balance_history`       | Raw balance snapshots over the last `hours`.                   |

## Doppler

This project uses Doppler for secrets from the shared `common` project
(config `dev`) — no per-repo Doppler project is created. First use (links
the shared project and `dev` config):

```bash
doppler setup --project common --config dev
```

If your repo needs app-specific secrets that shouldn't live in the shared
`common` project, regenerate it with the doppler capability set to
`projectStrategy: "new"` to get a dedicated project.

The Doppler CLI is installed in the devcontainer — it must be on PATH for the
VS Code extension and `doppler run` to work. Auth is persisted via the host
`~/.doppler` bind-mount.

### Env-var precedence (read this if `doppler run` hits the wrong project)

Doppler resolves its target as **environment variables > `doppler.yaml` >
`~/.doppler` scoped config**. If your shell — or the session that launched
the devcontainer (e.g. an agent runtime) — exports `DOPPLER_PROJECT` /
`DOPPLER_CONFIG` / `DOPPLER_ENVIRONMENT`, those silently override this
repo's `doppler.yaml` and every `doppler` command targets the wrong
project. The devcontainer's post-create setup pins this repo's context
(`common`/`dev`) in `~/.bashrc` and `~/.zshrc` and warns at
setup if resolution still mismatches. To force the correct context manually:

```bash
unset DOPPLER_PROJECT DOPPLER_CONFIG DOPPLER_ENVIRONMENT
doppler setup --no-interactive --project common --config dev
```

## Deployment

See `deploy/README.md` for the deployment runbook (CircleCI -> GHCR ->
Watchtower -> Docker host). Deploy with:

```bash
docker compose up -d
```

## Generated by genproj

This project was generated using the genproj tool.
