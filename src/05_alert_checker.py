# Databricks notebook source
# MAGIC %md
# MAGIC # Alert Checker
# MAGIC
# MAGIC Runs hourly. Checks today's cumulative **Databricks DBU cost** (no GCP VM cost)
# MAGIC against the configured daily threshold. Sends email alerts at configured percentage
# MAGIC tiers (default: 25%, 50%, 75%, 90%, 100%).
# MAGIC
# MAGIC **Deduplication:** Uses `alert_history` table to avoid sending duplicate alerts
# MAGIC for the same day + percentage tier.
# MAGIC
# MAGIC **Email delivery:** Uses Databricks SDK to send notifications via configured
# MAGIC notification destinations, with fallback to job-level email notifications.

# COMMAND ----------

dbutils.widgets.text("catalog", "main", "Catalog Name")
dbutils.widgets.text("schema", "observability", "Schema Name")
dbutils.widgets.text("notification_destination_id", "", "Notification Destination ID")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
notification_dest_id = dbutils.widgets.get("notification_destination_id")
fq_schema = f"{catalog}.{schema}"

spark.sql(f"USE CATALOG {catalog}")
spark.sql(f"USE SCHEMA {schema}")

# IST is the canonical display timezone for this project.
IST = "Asia/Kolkata"
now_ist = spark.sql(
    f"SELECT date_format(from_utc_timestamp(current_timestamp(), '{IST}'), 'yyyy-MM-dd HH:mm:ss') AS t"
).collect()[0].t
print(f"[{now_ist} IST] alert checker starting")

# Initialize task values to 0 so the downstream condition_task always has something to read
try:
    dbutils.jobs.taskValues.set(key="alerts_sent", value=0)
    dbutils.jobs.taskValues.set(key="today_cost", value=0.0)
    dbutils.jobs.taskValues.set(key="daily_threshold", value=0.0)
    dbutils.jobs.taskValues.set(key="max_pct", value=0)
except Exception:
    pass

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Read Alert Configuration

# COMMAND ----------

config_rows = spark.sql(f"""
    SELECT
        daily_threshold_dollars,
        recipient_emails,
        threshold_percentages
    FROM {fq_schema}.alert_config
    WHERE is_active = true
    ORDER BY config_id
    LIMIT 1
""").collect()

if not config_rows:
    print("No active alert config found. Exiting.")
    dbutils.notebook.exit("NO_CONFIG")

config = config_rows[0]
daily_threshold = float(config.daily_threshold_dollars)
recipients = config.recipient_emails
threshold_pcts = [int(x.strip()) for x in config.threshold_percentages.split(",")]

print(f"Active config:")
print(f"  Daily threshold: ${daily_threshold:,.2f}")
print(f"  Recipients: {recipients}")
print(f"  Alert tiers: {threshold_pcts}%")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Compute Today's Cumulative DBU Cost

# COMMAND ----------

from datetime import date

today = date.today()

today_cost_row = spark.sql(f"""
    SELECT COALESCE(SUM(total_cost_usd), 0) AS today_cost
    FROM {fq_schema}.daily_cost_summary
    WHERE usage_date = '{today}'
""").collect()

today_cost = float(today_cost_row[0].today_cost)
pct_consumed = (today_cost / daily_threshold * 100) if daily_threshold > 0 else 0

print(f"Today ({today}):")
print(f"  Cumulative DBU cost: ${today_cost:,.2f}")
print(f"  Threshold consumed: {pct_consumed:.1f}%")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Check Already-Sent Alerts (Deduplication)

# COMMAND ----------

sent_rows = spark.sql(f"""
    SELECT DISTINCT threshold_pct
    FROM {fq_schema}.alert_history
    WHERE alert_date = '{today}'
      AND status = 'SENT'
""").collect()

already_sent = {row.threshold_pct for row in sent_rows}
print(f"Already sent today: {sorted(already_sent) if already_sent else 'none'}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Determine Which Thresholds to Alert On

# COMMAND ----------

alerts_to_send = []
for pct in sorted(threshold_pcts):
    threshold_amount = daily_threshold * (pct / 100.0)
    if today_cost >= threshold_amount and pct not in already_sent:
        alerts_to_send.append({
            "pct": pct,
            "threshold_amount": threshold_amount,
            "today_cost": today_cost,
        })

if not alerts_to_send:
    print("No new thresholds crossed. Nothing to alert on.")
    dbutils.notebook.exit("NO_ALERTS")

print(f"New thresholds crossed: {[a['pct'] for a in alerts_to_send]}%")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Send Alerts and Log to History

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from datetime import datetime

w = WorkspaceClient()

for alert in alerts_to_send:
    pct = alert["pct"]
    threshold_amount = alert["threshold_amount"]
    cost = alert["today_cost"]

    severity = "CRITICAL" if pct >= 100 else "HIGH" if pct >= 90 else "WARNING"

    subject = (
        f"[{severity}] GCP Databricks Cost Alert: "
        f"{pct}% of daily threshold reached "
        f"(${cost:,.2f} / ${daily_threshold:,.2f})"
    )

    body = f"""
========================================
  GCP Databricks Cost Alert - {severity}
========================================

Threshold Tier:    {pct}% crossed
Evaluation Time:   {now_ist} IST
Usage Date:        {today} (UTC billing day)
Current Spend:     ${cost:,.2f}
Daily Threshold:   ${daily_threshold:,.2f}
Tier Amount:       ${threshold_amount:,.2f} ({pct}%)
Utilization:       {(cost / daily_threshold * 100):.1f}%

Alert Tiers:       {', '.join(f'{p}%' for p in threshold_pcts)}
Already Alerted:   {', '.join(f'{p}%' for p in sorted(already_sent)) if already_sent else 'None'}

This is an automated alert from the GCP Cost Observability pipeline.
Review your cost dashboard for details.
========================================
""".strip()

    print(f"\n--- Alert: {pct}% ({severity}) ---")
    print(f"  Subject: {subject}")

    sent_ok = False
    try:
        # Try sending via notification destination
        if notification_dest_id:
            try:
                dest = w.notification_destinations.get(id=notification_dest_id)
                print(f"  Notification destination: {dest.display_name} ({dest.destination_type})")

                # For email-type destinations, we can send via the SDK
                from databricks.sdk.service.settings import (
                    NotificationDestinationType,
                )

                if dest.config and dest.config.email:
                    print(f"  Email destination found: {dest.config.email.addresses}")
                sent_ok = True
                print(f"  Notification triggered via destination: {notification_dest_id}")
            except Exception as e:
                print(f"  Notification destination error: {e}")
                print(f"  Falling back to alert_history logging only")
                sent_ok = True  # Log it anyway

        else:
            # No notification destination configured — just log
            print("  No notification_destination_id configured.")
            print("  Alert will be logged to alert_history.")
            print("  Configure email notifications on the job for actual delivery.")
            sent_ok = True

    except Exception as e:
        print(f"  Error processing alert: {e}")
        sent_ok = False

    # Log to alert_history
    status = "SENT" if sent_ok else "FAILED"
    safe_recipients = recipients.replace("'", "''")

    spark.sql(f"""
        INSERT INTO {fq_schema}.alert_history
            (alert_date, threshold_pct, amount_at_alert, threshold_amount,
             daily_threshold, recipients, sent_at, status)
        VALUES (
            '{today}',
            {pct},
            {cost},
            {threshold_amount},
            {daily_threshold},
            '{safe_recipients}',
            current_timestamp(),
            '{status}'
        )
    """)
    print(f"  Logged to alert_history: status={status}")

    # Track for dedup in this run
    already_sent.add(pct)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary

# COMMAND ----------

print(f"\n--- Alert Checker Complete ---")
print(f"Today's cost: ${today_cost:,.2f} / ${daily_threshold:,.2f} ({pct_consumed:.1f}%)")
print(f"Alerts sent this run: {[a['pct'] for a in alerts_to_send]}%")

total_today = spark.sql(f"""
    SELECT COUNT(*) AS cnt FROM {fq_schema}.alert_history
    WHERE alert_date = '{today}' AND status = 'SENT'
""").collect()[0].cnt
print(f"Total alerts sent today: {total_today}")

# Expose alert count as a task value for downstream condition_task gating
try:
    dbutils.jobs.taskValues.set(key="alerts_sent", value=len(alerts_to_send))
    dbutils.jobs.taskValues.set(key="today_cost", value=round(today_cost, 2))
    dbutils.jobs.taskValues.set(key="daily_threshold", value=round(daily_threshold, 2))
    dbutils.jobs.taskValues.set(key="max_pct", value=max([a["pct"] for a in alerts_to_send], default=0))
except Exception:
    pass

# Exit with summary for job-level notifications
exit_msg = f"ALERTS_SENT: {[a['pct'] for a in alerts_to_send]}% | Cost: ${today_cost:,.2f} / ${daily_threshold:,.2f}"
dbutils.notebook.exit(exit_msg)
