# Databricks notebook source
# MAGIC %md
# MAGIC # Setup: Create Schema and Tables for GCP Cost Observability
# MAGIC
# MAGIC This notebook creates the `observability` schema and all required tables.
# MAGIC It is idempotent — safe to run multiple times.

# COMMAND ----------

dbutils.widgets.text("catalog", "main", "Catalog Name")
dbutils.widgets.text("schema", "observability", "Schema Name")
dbutils.widgets.text("daily_threshold_dollars", "1000", "Daily Cost Threshold (USD)")
dbutils.widgets.text("alert_recipient_emails", "", "Alert Recipient Emails (comma-separated)")
dbutils.widgets.text("threshold_percentages", "25,50,75,90,100", "Alert tier percentages")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
threshold = dbutils.widgets.get("daily_threshold_dollars")
recipients = dbutils.widgets.get("alert_recipient_emails")
tier_pcts = dbutils.widgets.get("threshold_percentages")

fq_schema = f"{catalog}.{schema}"
print(f"Setting up: {fq_schema}")
print(f"Default threshold: ${threshold}, Recipients: {recipients}, Tiers: {tier_pcts}%")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Create Schema (catalog must already exist)

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {fq_schema}")
spark.sql(f"USE CATALOG {catalog}")
spark.sql(f"USE SCHEMA {schema}")
print(f"Using {fq_schema}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Table 1: daily_cost_summary
# MAGIC Daily aggregated costs by SKU, joined with list prices.

# COMMAND ----------

spark.sql("""
CREATE TABLE IF NOT EXISTS daily_cost_summary (
  usage_date              DATE          COMMENT 'Date of usage',
  sku_name                STRING        COMMENT 'Databricks SKU name',
  billing_origin_product  STRING        COMMENT 'Databricks product (JOBS, SQL, ALL_PURPOSE_COMPUTE, etc.)',
  cloud                   STRING        COMMENT 'Cloud provider (GCP)',
  workspace_id            STRING        COMMENT 'Workspace ID',
  is_serverless           BOOLEAN       COMMENT 'Whether usage is serverless',
  total_dbus              DECIMAL(38,6) COMMENT 'Total DBUs consumed',
  list_price_per_dbu      DECIMAL(38,6) COMMENT 'List price per DBU at time of usage',
  total_cost_usd          DECIMAL(38,6) COMMENT 'Total cost in USD (DBUs x list price)',
  _etl_updated_at         TIMESTAMP     COMMENT 'Last ETL update timestamp'
)
USING DELTA
COMMENT 'Daily aggregated Databricks costs by SKU, joined with list prices from system tables'
PARTITIONED BY (usage_date)
""")
print("Created: daily_cost_summary")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Table 2: sku_daily_details
# MAGIC Detailed daily breakdown per SKU with 7d/30d rolling trends.

# COMMAND ----------

spark.sql("""
CREATE TABLE IF NOT EXISTS sku_daily_details (
  usage_date              DATE          COMMENT 'Date of usage',
  sku_name                STRING        COMMENT 'Databricks SKU name',
  billing_origin_product  STRING        COMMENT 'Databricks product',
  product_category        STRING        COMMENT 'Grouped category (Compute, SQL Warehouse, DLT, etc.)',
  is_serverless           BOOLEAN       COMMENT 'Whether usage is serverless',
  is_photon               BOOLEAN       COMMENT 'Whether SKU is Photon-based',
  total_dbus              DECIMAL(38,6) COMMENT 'Total DBUs consumed',
  total_cost_usd          DECIMAL(38,6) COMMENT 'Total cost in USD',
  cost_7d_avg             DECIMAL(38,6) COMMENT '7-day rolling average cost',
  cost_30d_avg            DECIMAL(38,6) COMMENT '30-day rolling average cost',
  cost_pct_change_7d      DECIMAL(10,2) COMMENT 'Percent change vs 7-day average',
  _etl_updated_at         TIMESTAMP     COMMENT 'Last ETL update timestamp'
)
USING DELTA
COMMENT 'Detailed daily breakdown per SKU with 7d/30d rolling trends'
PARTITIONED BY (usage_date)
""")
print("Created: sku_daily_details")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Table 3: gcp_vm_reference_prices
# MAGIC Static reference: Databricks node_type_id mapped to GCP VM list prices.

# COMMAND ----------

spark.sql("""
CREATE TABLE IF NOT EXISTS gcp_vm_reference_prices (
  node_type_id        STRING        COMMENT 'Databricks node type identifier',
  gcp_machine_type    STRING        COMMENT 'GCP Compute Engine machine type',
  vcpus               INT           COMMENT 'Number of vCPUs',
  memory_gb           DOUBLE        COMMENT 'Memory in GB',
  hourly_price_usd    DECIMAL(10,6) COMMENT 'On-demand hourly price in USD',
  region              STRING        COMMENT 'GCP region for pricing (default: us-central1)',
  price_source        STRING        COMMENT 'Source: GCP list price or estimated from component pricing',
  last_updated        TIMESTAMP     COMMENT 'When the price was last updated'
)
USING DELTA
COMMENT 'Static reference mapping Databricks node types to GCP VM list prices'
""")
print("Created: gcp_vm_reference_prices")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Table 4: daily_cost_with_vm_estimate
# MAGIC Per-cluster daily cost with estimated GCP VM infrastructure cost.

# COMMAND ----------

spark.sql("""
CREATE TABLE IF NOT EXISTS daily_cost_with_vm_estimate (
  usage_date              DATE          COMMENT 'Date of usage',
  cluster_id              STRING        COMMENT 'Databricks cluster ID',
  cluster_name            STRING        COMMENT 'Cluster name',
  sku_name                STRING        COMMENT 'Databricks SKU name',
  driver_node_type        STRING        COMMENT 'Driver node type ID',
  worker_node_type        STRING        COMMENT 'Worker node type ID',
  num_workers             BIGINT        COMMENT 'Number of workers (or max autoscale)',
  cluster_uptime_hours    DECIMAL(20,4) COMMENT 'Estimated cluster uptime in hours',
  dbu_cost_usd            DECIMAL(38,6) COMMENT 'Databricks DBU cost in USD',
  estimated_vm_cost_usd   DECIMAL(38,6) COMMENT 'Estimated GCP VM cost in USD',
  total_estimated_cost    DECIMAL(38,6) COMMENT 'DBU cost + estimated VM cost',
  driver_vm_type          STRING        COMMENT 'GCP machine type for driver',
  worker_vm_type          STRING        COMMENT 'GCP machine type for workers',
  vm_cost_source          STRING        COMMENT 'How VM cost was derived: node_timeline / cluster_config / BUNDLED_IN_DBU (serverless) / unknown',
  _etl_updated_at         TIMESTAMP     COMMENT 'Last ETL update timestamp'
)
USING DELTA
COMMENT 'Daily cluster cost with estimated GCP VM infrastructure prices'
PARTITIONED BY (usage_date)
""")
print("Created: daily_cost_with_vm_estimate")

# Schema migration: add vm_cost_source column on existing deployments (idempotent)
existing_cols = {r.col_name for r in spark.sql("DESCRIBE daily_cost_with_vm_estimate").collect()}
if "vm_cost_source" not in existing_cols:
    spark.sql("ALTER TABLE daily_cost_with_vm_estimate ADD COLUMN vm_cost_source STRING COMMENT 'node_timeline / cluster_config / BUNDLED_IN_DBU / unknown'")
    print("  Added column vm_cost_source")
else:
    print("  vm_cost_source already present")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Table 5: alert_config
# MAGIC Alert threshold configuration. Users update this table to change alert settings.

# COMMAND ----------

spark.sql("""
CREATE TABLE IF NOT EXISTS alert_config (
  config_id                  INT           COMMENT 'Configuration row ID',
  daily_threshold_dollars    DECIMAL(20,2) COMMENT 'Daily cost threshold in USD',
  recipient_emails           STRING        COMMENT 'Comma-separated email addresses',
  is_active                  BOOLEAN       COMMENT 'Whether this config is active',
  threshold_percentages      STRING        COMMENT 'Comma-separated alert percentage tiers (e.g. 25,50,75,90,100)',
  created_at                 TIMESTAMP     COMMENT 'Config creation time',
  updated_at                 TIMESTAMP     COMMENT 'Last update time'
)
USING DELTA
COMMENT 'Alert threshold configuration for daily cost monitoring'
""")
print("Created: alert_config")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Table 6: alert_history
# MAGIC Log of sent alerts for deduplication and audit trail.

# COMMAND ----------

spark.sql("""
CREATE TABLE IF NOT EXISTS alert_history (
  alert_id           BIGINT GENERATED ALWAYS AS IDENTITY COMMENT 'Auto-generated alert ID',
  alert_date         DATE          COMMENT 'Date the alert was for',
  threshold_pct      INT           COMMENT 'Threshold percentage crossed (25, 50, 75, 90, 100)',
  amount_at_alert    DECIMAL(20,2) COMMENT 'Cumulative cost at the time of alert',
  threshold_amount   DECIMAL(20,2) COMMENT 'Dollar amount for this percentage tier',
  daily_threshold    DECIMAL(20,2) COMMENT 'The daily threshold in effect',
  recipients         STRING        COMMENT 'Recipients the alert was sent to',
  sent_at            TIMESTAMP     COMMENT 'When the alert was sent',
  status             STRING        COMMENT 'SENT or FAILED'
)
USING DELTA
COMMENT 'Log of sent cost alerts for deduplication and audit'
""")
print("Created: alert_history")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Upsert Alert Configuration from `config.yml`
# MAGIC
# MAGIC MERGE keeps `alert_config` in sync with the values supplied at deploy time.
# MAGIC On the first run it inserts the row; on subsequent runs it overwrites threshold,
# MAGIC recipients, and tier percentages so a redeploy with new config takes effect.
# MAGIC `created_at` is preserved on updates.

# COMMAND ----------

# Escape single quotes for SQL safety
safe_recipients = recipients.replace("'", "''")
safe_tiers = tier_pcts.replace("'", "''")

spark.sql(f"""
    MERGE INTO alert_config AS target
    USING (
        SELECT
            1                                 AS config_id,
            CAST({threshold} AS DECIMAL(20,2)) AS daily_threshold_dollars,
            '{safe_recipients}'               AS recipient_emails,
            true                              AS is_active,
            '{safe_tiers}'                    AS threshold_percentages,
            current_timestamp()               AS created_at,
            current_timestamp()               AS updated_at
    ) AS source
    ON target.config_id = source.config_id
    WHEN MATCHED THEN UPDATE SET
        daily_threshold_dollars = source.daily_threshold_dollars,
        recipient_emails        = source.recipient_emails,
        is_active               = true,
        threshold_percentages   = source.threshold_percentages,
        updated_at              = source.updated_at
    WHEN NOT MATCHED THEN INSERT *
""")

print(f"alert_config upserted: threshold=${threshold}, recipients={recipients}, tiers={tier_pcts}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary

# COMMAND ----------

tables = spark.sql(f"SHOW TABLES IN {fq_schema}").select("tableName").collect()
print(f"\nTables in {fq_schema}:")
for t in tables:
    print(f"  - {t.tableName}")
print(f"\nSetup complete. {len(tables)} tables ready.")
