# Databricks notebook source
# MAGIC %md
# MAGIC # VM Cost Estimation (v2)
# MAGIC
# MAGIC Estimates GCP Compute Engine cost per cluster-day. Joins billing usage with
# MAGIC the most authoritative source available for each cluster:
# MAGIC
# MAGIC | Source | Used when | `vm_cost_source` column |
# MAGIC |---|---|---|
# MAGIC | `system.compute.node_timeline` | Has per-instance start/end + node_type for the day | `node_timeline` |
# MAGIC | `system.compute.clusters` | Cluster config exists, no node_timeline | `cluster_config` |
# MAGIC | (none) | Cluster has no config in either source | `unknown` |
# MAGIC | Serverless SKU heuristic | SKU contains SERVERLESS / REAL_TIME_INFERENCE / SQL_PRO_SERVERLESS | `BUNDLED_IN_DBU` (vm_cost = 0) |
# MAGIC
# MAGIC Serverless workloads have no separable VM cost — Databricks bundles infra into the DBU price.

# COMMAND ----------

dbutils.widgets.text("catalog", "main", "Catalog Name")
dbutils.widgets.text("schema", "observability", "Schema Name")
dbutils.widgets.text("cloud", "GCP", "Cloud (GCP/AWS/AZURE)")
dbutils.widgets.text("region", "us-central1", "Cloud Region")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
cloud = dbutils.widgets.get("cloud").upper()
region = dbutils.widgets.get("region")
fq_schema = f"{catalog}.{schema}"
spark.sql(f"USE CATALOG {catalog}")
spark.sql(f"USE SCHEMA {schema}")
print(f"Running VM Cost Estimation v2 into {fq_schema}.daily_cost_with_vm_estimate (cloud={cloud}, region={region})")

# COMMAND ----------

merge_result = spark.sql(f"""
    MERGE INTO {fq_schema}.daily_cost_with_vm_estimate AS target
    USING (
        WITH cluster_usage AS (
            -- Per (date, cluster_id, sku_name): aggregate DBU + billing-window hours + DBU $
            SELECT
                u.usage_date,
                u.usage_metadata.cluster_id AS cluster_id,
                u.sku_name,
                SUM(u.usage_quantity) AS total_dbus,
                SUM(TIMESTAMPDIFF(SECOND, u.usage_start_time, u.usage_end_time) / 3600.0) AS billing_hours,
                SUM(
                    u.usage_quantity
                    * COALESCE(p.pricing.effective_list.default, p.pricing.default, 0)
                ) AS dbu_cost_usd
            FROM system.billing.usage u
            LEFT JOIN system.billing.list_prices p
                ON  u.sku_name = p.sku_name
                AND u.cloud = p.cloud
                AND u.usage_start_time >= p.price_start_time
                AND (p.price_end_time IS NULL OR u.usage_start_time < p.price_end_time)
            WHERE u.cloud = '{cloud}'
                AND u.usage_metadata.cluster_id IS NOT NULL
                AND u.usage_date >= DATE_SUB(CURRENT_DATE(), 90)
            GROUP BY u.usage_date, u.usage_metadata.cluster_id, u.sku_name
        ),
        -- Authoritative source #1: per-day node hours from node_timeline (all instances, even deleted)
        node_hours AS (
            SELECT
                DATE(start_time) AS usage_date,
                cluster_id,
                SUM(CASE WHEN driver THEN TIMESTAMPDIFF(SECOND, start_time, end_time) / 3600.0 ELSE 0 END) AS driver_hours,
                SUM(CASE WHEN NOT driver THEN TIMESTAMPDIFF(SECOND, start_time, end_time) / 3600.0 ELSE 0 END) AS worker_hours,
                MAX(CASE WHEN driver THEN node_type END) AS nt_driver_type,
                MAX(CASE WHEN NOT driver THEN node_type END) AS nt_worker_type
            FROM system.compute.node_timeline
            WHERE start_time >= DATE_SUB(CURRENT_DATE(), 90)
            GROUP BY DATE(start_time), cluster_id
        ),
        -- Fallback source #2: latest cluster config from system.compute.clusters
        cluster_config AS (
            SELECT
                c.cluster_id,
                c.cluster_name,
                c.driver_node_type   AS cc_driver_type,
                c.worker_node_type   AS cc_worker_type,
                COALESCE(c.worker_count, c.max_autoscale_workers, 1) AS cc_num_workers,
                ROW_NUMBER() OVER (PARTITION BY c.cluster_id ORDER BY c.change_time DESC) AS rn
            FROM system.compute.clusters c
        ),
        latest_cfg AS (SELECT * FROM cluster_config WHERE rn = 1)
        SELECT
            cu.usage_date,
            cu.cluster_id,
            lc.cluster_name,
            cu.sku_name,
            COALESCE(nh.nt_driver_type, lc.cc_driver_type) AS driver_node_type,
            COALESCE(nh.nt_worker_type, lc.cc_worker_type) AS worker_node_type,
            COALESCE(lc.cc_num_workers, 1) AS num_workers,
            COALESCE(nh.driver_hours + nh.worker_hours, cu.billing_hours) AS cluster_uptime_hours,
            cu.dbu_cost_usd,
            -- VM cost: prefer node_timeline (real instance hours), then cluster_config, then 0
            CASE
                WHEN UPPER(cu.sku_name) LIKE '%SERVERLESS%'
                  OR UPPER(cu.sku_name) LIKE '%REAL_TIME_INFERENCE%'
                  OR UPPER(cu.sku_name) LIKE '%SQL_PRO%'
                  OR UPPER(cu.sku_name) LIKE '%MODEL_SERVING%' THEN 0
                WHEN nh.driver_hours IS NOT NULL THEN
                    ROUND(
                        nh.driver_hours * COALESCE(dvm.hourly_price_usd, 0)
                      + nh.worker_hours * COALESCE(wvm.hourly_price_usd, 0)
                    , 6)
                WHEN lc.cc_driver_type IS NOT NULL THEN
                    ROUND(
                        cu.billing_hours * (
                            COALESCE(dvm.hourly_price_usd, 0)
                          + COALESCE(lc.cc_num_workers, 1) * COALESCE(wvm.hourly_price_usd, 0)
                        )
                    , 6)
                ELSE 0
            END AS estimated_vm_cost_usd,
            -- Total = DBU cost + VM estimate (computed AFTER the CASE above; recomputed inline to avoid CTE ref)
            ROUND(
                cu.dbu_cost_usd
              + CASE
                    WHEN UPPER(cu.sku_name) LIKE '%SERVERLESS%'
                      OR UPPER(cu.sku_name) LIKE '%REAL_TIME_INFERENCE%'
                      OR UPPER(cu.sku_name) LIKE '%SQL_PRO%'
                      OR UPPER(cu.sku_name) LIKE '%MODEL_SERVING%' THEN 0
                    WHEN nh.driver_hours IS NOT NULL THEN
                        nh.driver_hours * COALESCE(dvm.hourly_price_usd, 0)
                      + nh.worker_hours * COALESCE(wvm.hourly_price_usd, 0)
                    WHEN lc.cc_driver_type IS NOT NULL THEN
                        cu.billing_hours * (
                            COALESCE(dvm.hourly_price_usd, 0)
                          + COALESCE(lc.cc_num_workers, 1) * COALESCE(wvm.hourly_price_usd, 0)
                        )
                    ELSE 0
                END
            , 6) AS total_estimated_cost,
            dvm.gcp_machine_type AS driver_vm_type,
            wvm.gcp_machine_type AS worker_vm_type,
            CASE
                WHEN UPPER(cu.sku_name) LIKE '%SERVERLESS%'
                  OR UPPER(cu.sku_name) LIKE '%REAL_TIME_INFERENCE%'
                  OR UPPER(cu.sku_name) LIKE '%SQL_PRO%'
                  OR UPPER(cu.sku_name) LIKE '%MODEL_SERVING%' THEN 'BUNDLED_IN_DBU'
                WHEN nh.driver_hours IS NOT NULL THEN 'node_timeline'
                WHEN lc.cc_driver_type IS NOT NULL THEN 'cluster_config'
                ELSE 'unknown'
            END AS vm_cost_source,
            current_timestamp() AS _etl_updated_at
        FROM cluster_usage cu
        LEFT JOIN node_hours nh
            ON cu.usage_date = nh.usage_date AND cu.cluster_id = nh.cluster_id
        LEFT JOIN latest_cfg lc
            ON cu.cluster_id = lc.cluster_id
        LEFT JOIN {fq_schema}.gcp_vm_reference_prices dvm
            ON COALESCE(nh.nt_driver_type, lc.cc_driver_type) = dvm.node_type_id AND dvm.region = '{region}'
        LEFT JOIN {fq_schema}.gcp_vm_reference_prices wvm
            ON COALESCE(nh.nt_worker_type, lc.cc_worker_type) = wvm.node_type_id AND wvm.region = '{region}'
    ) AS source
    ON  target.usage_date  = source.usage_date
    AND target.cluster_id  = source.cluster_id
    AND target.sku_name    = source.sku_name
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")

row_count = spark.sql(f"SELECT COUNT(*) AS cnt FROM {fq_schema}.daily_cost_with_vm_estimate").collect()[0].cnt
print(f"daily_cost_with_vm_estimate: {row_count} rows after MERGE")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary by vm_cost_source

# COMMAND ----------

now_ist = spark.sql(
    "SELECT date_format(from_utc_timestamp(current_timestamp(), 'Asia/Kolkata'), 'yyyy-MM-dd HH:mm:ss') AS t"
).collect()[0].t
print(f"\n[{now_ist} IST] VM cost estimation complete")

stats = spark.sql(f"""
    SELECT vm_cost_source,
           COUNT(*)                         AS rows,
           ROUND(SUM(dbu_cost_usd), 2)      AS dbu_cost,
           ROUND(SUM(estimated_vm_cost_usd), 2) AS vm_cost
    FROM {fq_schema}.daily_cost_with_vm_estimate
    GROUP BY vm_cost_source
    ORDER BY vm_cost DESC
""").collect()
for r in stats:
    print(f"  {r.vm_cost_source:20s} rows={r.rows:5d}  dbu=${r.dbu_cost or 0:>12,.2f}  vm=${r.vm_cost or 0:>10,.2f}")
