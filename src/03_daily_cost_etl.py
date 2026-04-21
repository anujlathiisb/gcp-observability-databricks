# Databricks notebook source
# MAGIC %md
# MAGIC # Daily Cost ETL
# MAGIC
# MAGIC Joins `system.billing.usage` with `system.billing.list_prices` to produce gold cost tables:
# MAGIC 1. **daily_cost_summary** — daily aggregated costs by SKU
# MAGIC 2. **sku_daily_details** — per-SKU breakdown with 7d/30d rolling trends
# MAGIC
# MAGIC Uses MERGE (upsert) for idempotency — safe to re-run.

# COMMAND ----------

dbutils.widgets.text("catalog", "main", "Catalog Name")
dbutils.widgets.text("schema", "observability", "Schema Name")
dbutils.widgets.text("cloud", "GCP", "Cloud (GCP/AWS/AZURE)")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
cloud = dbutils.widgets.get("cloud").upper()
fq_schema = f"{catalog}.{schema}"
spark.sql(f"USE CATALOG {catalog}")
spark.sql(f"USE SCHEMA {schema}")
print(f"Running Daily Cost ETL into {fq_schema} (cloud={cloud})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: MERGE into daily_cost_summary
# MAGIC
# MAGIC Aggregates DBU consumption by (usage_date, sku_name, billing_origin_product, workspace_id, is_serverless)
# MAGIC and joins with `system.billing.list_prices` using SCD2 temporal join for accurate pricing.

# COMMAND ----------

merge_summary = spark.sql(f"""
    MERGE INTO {fq_schema}.daily_cost_summary AS target
    USING (
        SELECT
            u.usage_date,
            u.sku_name,
            u.billing_origin_product,
            u.cloud,
            CAST(u.workspace_id AS STRING) AS workspace_id,
            COALESCE(
                u.product_features.is_serverless,
                false
            ) AS is_serverless,
            SUM(u.usage_quantity) AS total_dbus,
            -- SCD2 join: find the price row active at usage time
            MAX(COALESCE(p.pricing.effective_list.default, p.pricing.default, 0)) AS list_price_per_dbu,
            SUM(u.usage_quantity)
                * MAX(COALESCE(p.pricing.effective_list.default, p.pricing.default, 0))
                AS total_cost_usd,
            current_timestamp() AS _etl_updated_at
        FROM system.billing.usage u
        LEFT JOIN system.billing.list_prices p
            ON u.sku_name = p.sku_name
            AND u.cloud = p.cloud
            AND u.usage_start_time >= p.price_start_time
            AND (p.price_end_time IS NULL OR u.usage_start_time < p.price_end_time)
        WHERE u.cloud = '{cloud}'
            AND u.usage_date >= DATE_SUB(CURRENT_DATE(), 90)
        GROUP BY
            u.usage_date,
            u.sku_name,
            u.billing_origin_product,
            u.cloud,
            CAST(u.workspace_id AS STRING),
            COALESCE(u.product_features.is_serverless, false)
    ) AS source
    ON  target.usage_date              = source.usage_date
    AND target.sku_name                = source.sku_name
    AND target.workspace_id            = source.workspace_id
    AND target.billing_origin_product  = source.billing_origin_product
    AND target.is_serverless           = source.is_serverless
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")

row_count = spark.sql(f"SELECT COUNT(*) AS cnt FROM {fq_schema}.daily_cost_summary").collect()[0].cnt
print(f"daily_cost_summary: {row_count} rows after MERGE")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: MERGE into sku_daily_details
# MAGIC
# MAGIC Reads from daily_cost_summary, groups by SKU, adds:
# MAGIC - Product category classification
# MAGIC - Photon detection
# MAGIC - 7-day and 30-day rolling cost averages
# MAGIC - Percent change vs 7-day average

# COMMAND ----------

merge_details = spark.sql(f"""
    MERGE INTO {fq_schema}.sku_daily_details AS target
    USING (
        WITH base AS (
            SELECT
                usage_date,
                sku_name,
                billing_origin_product,
                CASE
                    WHEN billing_origin_product IN ('JOBS', 'ALL_PURPOSE_COMPUTE', 'JOBS_COMPUTE')
                        THEN 'Compute'
                    WHEN billing_origin_product IN ('SQL', 'SQL_WAREHOUSE', 'SERVERLESS_SQL')
                        THEN 'SQL Warehouse'
                    WHEN billing_origin_product IN ('DLT', 'DELTA_LIVE_TABLES')
                        THEN 'Delta Live Tables'
                    WHEN billing_origin_product IN ('MODEL_SERVING', 'FOUNDATION_MODEL_APIS')
                        THEN 'Model Serving'
                    WHEN billing_origin_product IN ('VECTOR_SEARCH')
                        THEN 'Vector Search'
                    WHEN billing_origin_product IN ('INTERACTIVE', 'NOTEBOOKS')
                        THEN 'Interactive'
                    ELSE 'Other'
                END AS product_category,
                is_serverless,
                CASE
                    WHEN UPPER(sku_name) LIKE '%PHOTON%' THEN true
                    ELSE false
                END AS is_photon,
                SUM(total_dbus) AS total_dbus,
                SUM(total_cost_usd) AS total_cost_usd
            FROM {fq_schema}.daily_cost_summary
            WHERE usage_date >= DATE_SUB(CURRENT_DATE(), 90)
            GROUP BY
                usage_date, sku_name, billing_origin_product, is_serverless
        ),
        with_trends AS (
            SELECT
                b.*,
                AVG(b.total_cost_usd) OVER (
                    PARTITION BY b.sku_name
                    ORDER BY b.usage_date
                    ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
                ) AS cost_7d_avg,
                AVG(b.total_cost_usd) OVER (
                    PARTITION BY b.sku_name
                    ORDER BY b.usage_date
                    ROWS BETWEEN 29 PRECEDING AND CURRENT ROW
                ) AS cost_30d_avg
            FROM base b
        )
        SELECT
            usage_date,
            sku_name,
            billing_origin_product,
            product_category,
            is_serverless,
            is_photon,
            total_dbus,
            total_cost_usd,
            cost_7d_avg,
            cost_30d_avg,
            CASE
                WHEN cost_7d_avg > 0 THEN
                    ROUND((total_cost_usd - cost_7d_avg) / cost_7d_avg * 100, 2)
                ELSE 0
            END AS cost_pct_change_7d,
            current_timestamp() AS _etl_updated_at
        FROM with_trends
        WHERE usage_date >= DATE_SUB(CURRENT_DATE(), 90)
    ) AS source
    ON  target.usage_date              = source.usage_date
    AND target.sku_name                = source.sku_name
    AND target.billing_origin_product  = source.billing_origin_product
    AND target.is_serverless           = source.is_serverless
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")

row_count = spark.sql(f"SELECT COUNT(*) AS cnt FROM {fq_schema}.sku_daily_details").collect()[0].cnt
print(f"sku_daily_details: {row_count} rows after MERGE")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary

# COMMAND ----------

print("\n--- Daily Cost ETL Complete ---")
for table in ["daily_cost_summary", "sku_daily_details"]:
    stats = spark.sql(f"""
        SELECT
            COUNT(*) AS rows,
            MIN(usage_date) AS min_date,
            MAX(usage_date) AS max_date,
            SUM(total_cost_usd) AS total_cost
        FROM {fq_schema}.{table}
    """).collect()[0]
    print(f"\n{table}:")
    print(f"  Rows: {stats.rows}")
    print(f"  Date range: {stats.min_date} to {stats.max_date}")
    print(f"  Total cost: ${stats.total_cost:,.2f}")
