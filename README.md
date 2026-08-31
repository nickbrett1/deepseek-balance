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
- **Spend per interval** — a color-coded bar chart of how much was spent in each
  time slice of the last `RAPID_WINDOW_MINUTES`: green = under expectations,
  blue = about right, orange = above expectations (vs. the per-slice share of
  your typical daily spend). Bar width is `SPEND_SLICE_MINUTES`.
- **Abnormal rapid drops?** — scans the last `RAPID_WINDOW_MINUTES` (default 60)
  for single-interval declines that are both a meaningful percentage
  (`RAPID_MIN_PCT`, default 2%) and a clear outlier relative to the recent
  median per-interval drop (`RAPID_MULT`, default 2×). Any hits surface as a
  red alert with the largest drop; otherwise a calm "no abnormal drops" line.

To catch rapid drops reliably the poller defaults to `POLL_INTERVAL=1m`
(granular enough to notice a sudden single-interval decline). Tune the
detection without touching code via these environment variables:

| Env var               | Default | Meaning                                            |
| --------------------- | ------- | -------------------------------------------------- |
| `POLL_INTERVAL`       | `1m`    | Balance polling cadence (finer = catches faster drops). |
| `RAPID_WINDOW_MINUTES`| `60`    | How far back to look for rapid drops.              |
| `RAPID_MIN_PCT`       | `0.02`  | Min single-drop % to consider (as a fraction).     |
| `RAPID_MULT`          | `2.0`   | Outlier multiple vs the recent median drop.        |
| `MAX_GAP_MINUTES`     | `30`    | Skip comparing drops across gaps wider than this.  |
| `NORMAL_DAYS`         | `14`    | Days of history used for the "typical day" baseline. |
| `SPEND_SLICE_MINUTES` | `5`     | Width of each bar in the "spend last hour" chart.  |

The data endpoint is `/balance/daily` (accepts an optional `now` query param
carrying the client's local time so "today" matches the viewer's timezone).

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
