# Install Guide — from zero to running in ~10 minutes

This project is configured from a single file (`config.yml`) and deployed with a single command (`./scripts/deploy.py`). The steps below walk you through a clean install on any Databricks workspace.

## 0. Prerequisites

On your laptop:
- Databricks CLI v0.230+ (`brew install databricks/tap/databricks`)
- Python 3.9+ with PyYAML (`pip install pyyaml`)

In the target workspace:
- Unity Catalog enabled
- `system.billing.usage`, `system.billing.list_prices`, `system.compute.clusters`, `system.compute.node_types` enabled (account-admin)
- A serverless SQL warehouse
- An existing catalog you can `CREATE SCHEMA` in

Smoke-test system tables in the workspace:
```sql
SELECT count(*) FROM system.billing.usage  WHERE usage_date >= date_sub(current_date(), 7);
SELECT count(*) FROM system.compute.clusters;
```

## 1. Authenticate the CLI

```bash
databricks auth login --host https://<your-workspace-url>
# Pick a profile name, e.g. "my-workspace"
databricks auth profiles | grep my-workspace   # sanity check
```

## 2. Clone / copy the bundle

```bash
cp -r /path/to/gcp-cost-observability ~/Desktop/my-cost-obs
cd ~/Desktop/my-cost-obs
```

## 3. Edit `config.yml`

This is the **only** user-editable config file. Fill in these keys:

```yaml
workspace_host: "https://<your-workspace-url>"
databricks_profile: "my-workspace"

catalog: "main"
schema: "observability"

cloud: "GCP"                      # or AWS / AZURE
region: "asia-south1"             # whatever region your workspace runs in

daily_threshold_dollars: 2000
threshold_percentages: "25,50,75,90,100"
alert_recipient_emails:
  - ops@company.com
  - finance@company.com

schedule_timezone: "Asia/Kolkata" # IST
etl_schedule_cron:   "0 0 6 * * ?"
alert_schedule_cron: "0 0 * * * ?"

# Optional — uncomment when you have a GCP Cloud Billing API key in a secret scope
# gcp_billing_api_key_secret_scope: "my-scope"
# gcp_billing_api_key_secret_name:  "gcp-billing-api-key"
```

## 4. One-command deploy

```bash
./scripts/deploy.py --target=prod --run-etl
```

What happens:
1. Writes `local_overrides.yml` with `workspace.host` + every variable for the target (gitignored)
2. Runs `scripts/prepare.sh` to render the Lakeview dashboard template with your catalog/schema
3. Runs `databricks bundle deploy --target=prod --force --auto-approve --profile=<your-profile>`
4. Kicks off `cost_pipeline` so the tables populate and the dashboard has data immediately (because of `--run-etl`)

Expected output ends with:
```
[deploy.py] Done. Target: prod
```

Re-running `./scripts/deploy.py` is idempotent — DABs will MERGE the job / dashboard state.

## 5. Verify

```bash
databricks bundle summary --target prod --profile my-workspace
```

You should see:
- Dashboards: `[prod] GCP Cost Observability` with a URL
- Jobs: `cost_pipeline` + `alert_pipeline`

Sanity-check data in SQL:
```sql
SHOW TABLES IN main.observability;   -- 6 tables
SELECT COUNT(*) FROM main.observability.daily_cost_summary;
SELECT COUNT(*) FROM main.observability.gcp_vm_reference_prices;
SELECT * FROM main.observability.alert_config;
```

Open the dashboard:
```bash
databricks bundle open cost_observability_dashboard --target prod --profile my-workspace
```

## 6. Test the alert email path

Set the threshold to something your current day already crossed:
```sql
UPDATE main.observability.alert_config
SET daily_threshold_dollars = 0.01, updated_at = current_timestamp()
WHERE config_id = 1;
```

Run the alert pipeline once:
```bash
databricks bundle run alert_pipeline --target prod --profile my-workspace
```

You should receive an email from `noreply@databricks.com`. Restore the real threshold afterwards.

## 7. Switch months on the dashboard

The **Month** dropdown at the top-right of Cost Overview defaults to the current month. Pick any prior month to see month-scoped metrics across Cost Overview / SKU Deep Dive / GCP VM Costs. Alerts & Config always shows today's real-time numbers — it doesn't respond to the month filter.

---

## Troubleshooting

### `PyYAML required` from deploy.py
```bash
pip install pyyaml
# or python3 -m pip install pyyaml
```

### `the host in the profile doesn't match the host configured in the bundle`
`local_overrides.yml` wasn't generated yet (first-run before `deploy.py`) or you ran `databricks bundle deploy` directly without running `deploy.py` first. Always use `./scripts/deploy.py`.

### `cannot update dashboard: validation failed`
You ran `databricks bundle deploy` without first running `prepare.sh` to render the dashboard template. Use `./scripts/deploy.py` instead (it does both).

### Alert emails never arrive
Check in order:
1. `SELECT * FROM alert_history WHERE alert_date = current_date()` — empty means no tier crossed
2. Job run for `alert_pipeline` — did `send_alert_email` task execute or was it skipped? (Skipped means no tier crossed; that's correct.)
3. `alert_recipient_emails` in `config.yml` — non-empty and redeployed?
4. Check spam — Databricks emails come from `noreply@databricks.com`

### Live GCP pricing API returns 0 rows
Make sure your GCP API key has `cloudbilling.services.skus.list` permission and is active. Re-run `databricks bundle run cost_pipeline --target prod`. The refresh_vm_prices task log shows the scan count and matched count.

### Dashboard month filter is empty
The dropdown is populated from `ds_month_picker` which reads `daily_cost_summary`. If the table is empty, run the ETL first (`databricks bundle run cost_pipeline`).

### Cron runs at the wrong time
Timezone is `Asia/Kolkata` by default. If you need a different zone, change `schedule_timezone` in `config.yml` and redeploy. Quartz cron format: `sec min hour dom mon dow [year]`.

## Uninstall

```bash
databricks bundle destroy --target prod --profile my-workspace
DROP SCHEMA main.observability CASCADE;   # only if you want the data gone
```
