# Databricks notebook source
# MAGIC %md
# MAGIC # Alert Email Notifier
# MAGIC
# MAGIC Runs only when the upstream `check_alerts` task set `alerts_sent > 0`.
# MAGIC Task-level `email_notifications.on_success` on this task delivers the
# MAGIC actual email to the configured recipients.
# MAGIC
# MAGIC The notebook body prints the alert context so that it appears in the
# MAGIC job-run detail linked from the email.

# COMMAND ----------

dbutils.widgets.text("catalog", "main", "Catalog Name")
dbutils.widgets.text("schema", "observability", "Schema Name")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
fq_schema = f"{catalog}.{schema}"

# COMMAND ----------

# Pull task values from the upstream check_alerts task
try:
    alerts_sent = dbutils.jobs.taskValues.get(
        taskKey="check_alerts", key="alerts_sent", default=0, debugValue=0
    )
    today_cost = dbutils.jobs.taskValues.get(
        taskKey="check_alerts", key="today_cost", default=0.0, debugValue=0.0
    )
    daily_threshold = dbutils.jobs.taskValues.get(
        taskKey="check_alerts", key="daily_threshold", default=0.0, debugValue=0.0
    )
    max_pct = dbutils.jobs.taskValues.get(
        taskKey="check_alerts", key="max_pct", default=0, debugValue=0
    )
except Exception:
    alerts_sent, today_cost, daily_threshold, max_pct = 0, 0.0, 0.0, 0

pct_consumed = (today_cost / daily_threshold * 100) if daily_threshold else 0
now_ist = spark.sql(
    "SELECT date_format(from_utc_timestamp(current_timestamp(), 'Asia/Kolkata'), 'yyyy-MM-dd HH:mm:ss') AS t"
).collect()[0].t

print("=" * 60)
print(f"  GCP Databricks Cost Alert Triggered — {now_ist} IST")
print("=" * 60)
print(f"  New alert tiers crossed this run: {alerts_sent}")
print(f"  Highest tier crossed:             {max_pct}%")
print(f"  Today's cumulative DBU cost:      ${today_cost:,.2f}")
print(f"  Daily threshold:                  ${daily_threshold:,.2f}")
print(f"  Threshold consumed:               {pct_consumed:.1f}%")
print("=" * 60)
print()
print("Recent alert history (last 5 rows, times in IST):")
try:
    recent = spark.sql(f"""
        SELECT alert_date,
               threshold_pct,
               amount_at_alert,
               threshold_amount,
               daily_threshold,
               recipients,
               date_format(from_utc_timestamp(sent_at, 'Asia/Kolkata'), 'yyyy-MM-dd HH:mm:ss') AS sent_at_ist,
               status
        FROM {fq_schema}.alert_history
        ORDER BY sent_at DESC
        LIMIT 5
    """).collect()
    for r in recent:
        print(
            f"  {r.sent_at_ist} IST  tier={r.threshold_pct}%  "
            f"cost=${float(r.amount_at_alert):,.2f}  "
            f"threshold=${float(r.daily_threshold):,.2f}  status={r.status}"
        )
except Exception as e:
    print(f"  (could not read alert_history: {e})")
