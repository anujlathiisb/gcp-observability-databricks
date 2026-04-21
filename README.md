# GCP Cost Observability — Databricks Asset Bundle

A single-folder, single-config deployment of a Databricks cost monitoring solution: a 4-page Lakeview dashboard, an hourly threshold-alert pipeline, a daily ETL, and daily-refreshed GCP VM cost estimates. Deployable to any Databricks workspace (GCP / AWS / Azure) with Unity Catalog.

> **To deploy:**
> - **[DEPLOY.md](DEPLOY.md)** — canonical CLI guide. One command (`./scripts/deploy.py`), 2 minutes.
> - **[DEPLOY_UI.md](DEPLOY_UI.md)** — UI-only guide. Git Folder + web terminal, or fully manual (SQL editor + Workflows UI + Dashboard import). For environments where the CLI isn't available.

## What you get

1. **Lakeview Dashboard** — 4 pages, all filterable by month:
   - **Cost Overview** — Month Total, Avg Daily, Peak Day, vs Prev Month, daily trend, top SKUs
   - **SKU Deep Dive** — per-SKU daily cost, 7d/30d rolling averages, SKU/Category filters
   - **GCP VM Costs** — VM Cost, Cluster DBU Cost, TCO, Avg Daily VM, DBU-vs-VM trend, cost by node type, per-cluster table, reference price table
   - **Alerts & Config** — month spend, avg-daily-vs-threshold %, daily threshold, full alert history

2. **Daily ETL** (`cost_pipeline`, 6 AM IST) — 4 tasks:
   - `setup_schema` · DDL + upserts `alert_config` from `config.yml`
   - `refresh_vm_prices` · daily GCP VM list-price refresh (live API or region-aware seed)
   - `daily_cost_etl` · billing × list_prices → gold tables
   - `vm_cost_estimation` · node_timeline + cluster config + serverless heuristic → per-cluster VM cost with `vm_cost_source` lineage

3. **Hourly Alert** (`alert_pipeline`, every hour IST) — check → gate → email on tier crossed, dedup'd via `alert_history`.

4. **Single config file** — edit `config.yml` (workspace URL, catalog, region, threshold, emails, cron); one command to deploy.

## Quick start

**One-command install** (clones, authenticates, writes `config.yml`, deploys):
```bash
curl -fsSL https://raw.githubusercontent.com/anujlathiisb/gcp-observability-databricks/main/install.sh | bash -s -- \
  --workspace-host="https://<your-workspace>" \
  --profile="my-workspace" --catalog="main" \
  --region="us-central1" --emails="ops@example.com"
```

**Or explicit 3-step flow:**
```bash
cp config.example.yml config.yml
$EDITOR config.yml                       # fill in workspace + catalog + emails
./scripts/deploy.py --target=prod --run-etl
```

Full guide with flag reference, UI-only path, and troubleshooting: [DEPLOY.md](DEPLOY.md) / [DEPLOY_UI.md](DEPLOY_UI.md).

## Folder contents

```
config.example.yml         ← copy to config.yml, edit, deploy
config.yml                 ← YOUR config (gitignored)
DEPLOY.md                  ← canonical deployment guide
INSTALL.md                 ← longer walkthrough (same content, more prose)
databricks.yml             ← DABs bundle config (don't edit)
scripts/deploy.py          ← one-command deploy (calls prepare.sh + bundle deploy)
scripts/prepare.sh         ← renders dashboard template for your catalog/schema
resources/                 ← DABs job + dashboard resource definitions
src/                       ← Notebooks (setup, ETL, alerts) + dashboard template
```

## Deployment targets

| Target | Mode | Notes |
|---|---|---|
| `dev` | development | DABs auto-prefixes resource names with `[dev <user>]` |
| `staging` | default | `observability_staging` schema |
| `prod` | production | Shared, `admins: CAN_MANAGE`, `users: CAN_VIEW` |

## Requirements

- Databricks workspace with Unity Catalog
- System tables enabled: `system.billing.usage`, `system.billing.list_prices`, `system.compute.clusters`, `system.compute.node_types`, `system.compute.node_timeline`
- A serverless SQL warehouse in the workspace
- Databricks CLI v0.230+ with `databricks auth login` completed
- Python 3.9+ with PyYAML (`pip install pyyaml`) on your laptop
- An existing UC catalog you can write to (the bundle creates the schema inside it)

## Core design decisions

- **IST everywhere** — schedules and all displayed timestamps are `Asia/Kolkata`. `usage_date` remains UTC (Databricks' canonical billing day).
- **Single source of truth** — all configuration lives in `config.yml`. `deploy.py` materializes it into bundle variables + dashboard template substitutions so nothing else needs editing.
- **Every deploy upserts `alert_config`** — threshold, tiers, recipients rotate on redeploy.
- **VM cost lineage (`vm_cost_source`)** — `node_timeline` (most accurate) → `cluster_config` (fallback) → `BUNDLED_IN_DBU` (serverless, $0 by design) → `unknown` (Databricks system-tables coverage gap).
- **Don't edit the dashboard layout in the template** — user controls widget positions in the Lakeview UI; automation only touches query/field/spec content.

## Extending

- **Slack alerts** — configure a workspace Slack notification destination, set `notification_destination_id` variable
- **AWS / Azure** — set `cloud` in `config.yml`; for live pricing add seeded lists / APIs in `02_refresh_vm_prices.py`
- **Tag allocation** — join `system.billing.usage.custom_tags` to break costs down by team/project
- **Longer retention** — the month-picker dropdown shows up to 365 days of months; extend the lookback in `03_daily_cost_etl.py` / `04_vm_cost_estimation.py` for older data
