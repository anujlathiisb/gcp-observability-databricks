# Databricks notebook source
# MAGIC %md
# MAGIC # Refresh Cloud VM Reference Prices (daily)
# MAGIC
# MAGIC Populates `gcp_vm_reference_prices` with list prices for every VM type
# MAGIC actually used in the workspace + a seeded list of common types.
# MAGIC
# MAGIC **Logic:**
# MAGIC 1. If `cloud=GCP` and a Cloud Billing API key secret is configured,
# MAGIC    fetch live GCP Compute Engine list prices from
# MAGIC    [cloudbilling.googleapis.com](https://cloud.google.com/billing/docs/reference/catalog)
# MAGIC    for the given `region`.
# MAGIC 2. Otherwise, MERGE a built-in seed list of common GCP machine types
# MAGIC    (N1 / N2 / N2D / E2 / C2) with prices sourced from
# MAGIC    [cloud.google.com/products/compute/pricing](https://cloud.google.com/products/compute/pricing).
# MAGIC 3. For any node type present in `system.compute.node_types` but not in
# MAGIC    `gcp_vm_reference_prices`, insert an estimated row using GCP N1
# MAGIC    component pricing ($0.04749/vCPU/hr + $0.006383/GB/hr).
# MAGIC
# MAGIC This task runs daily as part of `cost_pipeline` so prices stay current.

# COMMAND ----------

dbutils.widgets.text("catalog", "main", "Catalog Name")
dbutils.widgets.text("schema", "observability", "Schema Name")
dbutils.widgets.text("cloud", "GCP", "Cloud (GCP/AWS/AZURE)")
dbutils.widgets.text("region", "us-central1", "Cloud Region")
dbutils.widgets.text("gcp_billing_api_key_secret_scope", "", "GCP Billing API secret scope (optional)")
dbutils.widgets.text("gcp_billing_api_key_secret_name", "", "GCP Billing API secret name (optional)")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
cloud = dbutils.widgets.get("cloud").upper()
region = dbutils.widgets.get("region")
api_scope = dbutils.widgets.get("gcp_billing_api_key_secret_scope")
api_name = dbutils.widgets.get("gcp_billing_api_key_secret_name")

spark.sql(f"USE CATALOG {catalog}")
spark.sql(f"USE SCHEMA {schema}")
fq_schema = f"{catalog}.{schema}"
print(f"Refreshing VM prices in {fq_schema} | cloud={cloud} region={region}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Try live GCP Cloud Billing Catalog API (optional)

# COMMAND ----------

from pyspark.sql import Row
from pyspark.sql import functions as F

live_rows = []
live_fetched = False

if cloud == "GCP" and api_scope and api_name:
    try:
        import requests

        api_key = dbutils.secrets.get(scope=api_scope, key=api_name)
        # Compute Engine service ID in the GCP Cloud Billing Catalog
        service_id = "6F81-5844-456A"
        url = f"https://cloudbilling.googleapis.com/v1/services/{service_id}/skus"

        print(f"Fetching live GCP prices for region={region} ...")
        token = None
        n_skus = 0
        while True:
            params = {"key": api_key, "pageSize": 5000}
            if token:
                params["pageToken"] = token
            resp = requests.get(url, params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            for sku in data.get("skus", []):
                n_skus += 1
                cat = sku.get("category", {}) or {}
                if cat.get("resourceFamily") != "Compute":
                    continue
                if cat.get("usageType") != "OnDemand":
                    continue
                if region not in (sku.get("serviceRegions") or []):
                    continue
                desc = (sku.get("description") or "").lower()
                tier_info = ((sku.get("pricingInfo") or [{}])[0].get("pricingExpression") or {}).get("tieredRates") or []
                if not tier_info:
                    continue
                unit = tier_info[0].get("unitPrice", {})
                nanos = unit.get("nanos", 0)
                units = int(unit.get("units", 0))
                price_per_hour = units + nanos / 1e9
                if price_per_hour <= 0:
                    continue
                live_rows.append(
                    Row(
                        node_type_id=sku.get("name", "").split("/")[-1],
                        gcp_machine_type=desc,
                        vcpus=0,
                        memory_gb=0.0,
                        hourly_price_usd=float(price_per_hour),
                        region=region,
                        price_source=f"GCP Cloud Billing Catalog API ({region})",
                    )
                )
            token = data.get("nextPageToken")
            if not token:
                break
        live_fetched = len(live_rows) > 0
        print(f"  scanned {n_skus} SKUs — {len(live_rows)} matched Compute/OnDemand/{region}")
    except Exception as e:
        print(f"  live fetch failed, falling back to seeded prices: {e}")
else:
    print("  Live GCP pricing API not configured (gcp_billing_api_key_secret_scope/_name empty)")
    print("  Using seeded static prices below.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Built-in seeded prices (fallback / always-present for common types)
# MAGIC Region-aware: static prices below are for `us-central1`. For other regions,
# MAGIC either configure the live API (Step 1) or update these numbers.

# COMMAND ----------

# (node_type_id, gcp_machine_type, vcpus, memory_gb, hourly_price_usd)
# On-demand list prices for us-central1 (source: cloud.google.com/products/compute/pricing)
seed_prices = [
    # N1 Standard
    ("n1-standard-4",   "n1-standard-4",   4,   15.0,   0.190000),
    ("n1-standard-8",   "n1-standard-8",   8,   30.0,   0.380000),
    ("n1-standard-16",  "n1-standard-16",  16,  60.0,   0.760000),
    ("n1-standard-32",  "n1-standard-32",  32,  120.0,  1.520000),
    ("n1-standard-64",  "n1-standard-64",  64,  240.0,  3.040000),
    ("n1-standard-96",  "n1-standard-96",  96,  360.0,  4.560000),
    # N1 Highmem
    ("n1-highmem-4",    "n1-highmem-4",    4,   26.0,   0.236000),
    ("n1-highmem-8",    "n1-highmem-8",    8,   52.0,   0.473000),
    ("n1-highmem-16",   "n1-highmem-16",   16,  104.0,  0.946000),
    ("n1-highmem-32",   "n1-highmem-32",   32,  208.0,  1.892000),
    ("n1-highmem-64",   "n1-highmem-64",   64,  416.0,  3.784000),
    ("n1-highmem-96",   "n1-highmem-96",   96,  624.0,  5.676000),
    # N2 Standard
    ("n2-standard-4",   "n2-standard-4",   4,   16.0,   0.194200),
    ("n2-standard-8",   "n2-standard-8",   8,   32.0,   0.388500),
    ("n2-standard-16",  "n2-standard-16",  16,  64.0,   0.776900),
    ("n2-standard-32",  "n2-standard-32",  32,  128.0,  1.553900),
    ("n2-standard-48",  "n2-standard-48",  48,  192.0,  2.330800),
    ("n2-standard-64",  "n2-standard-64",  64,  256.0,  3.107800),
    ("n2-standard-80",  "n2-standard-80",  80,  320.0,  3.884700),
    # N2 Highmem
    ("n2-highmem-4",    "n2-highmem-4",    4,   32.0,   0.262000),
    ("n2-highmem-8",    "n2-highmem-8",    8,   64.0,   0.524100),
    ("n2-highmem-16",   "n2-highmem-16",   16,  128.0,  1.048200),
    ("n2-highmem-32",   "n2-highmem-32",   32,  256.0,  2.096400),
    # N2D Standard
    ("n2d-standard-4",  "n2d-standard-4",  4,   16.0,   0.169000),
    ("n2d-standard-8",  "n2d-standard-8",  8,   32.0,   0.338000),
    ("n2d-standard-16", "n2d-standard-16", 16,  64.0,   0.676000),
    ("n2d-standard-32", "n2d-standard-32", 32,  128.0,  1.352000),
    # E2 Standard
    ("e2-standard-4",   "e2-standard-4",   4,   16.0,   0.134000),
    ("e2-standard-8",   "e2-standard-8",   8,   32.0,   0.268100),
    ("e2-standard-16",  "e2-standard-16",  16,  64.0,   0.536200),
    ("e2-standard-32",  "e2-standard-32",  32,  128.0,  1.072400),
    # C2 Standard
    ("c2-standard-4",   "c2-standard-4",   4,   16.0,   0.208800),
    ("c2-standard-8",   "c2-standard-8",   8,   32.0,   0.417600),
    ("c2-standard-16",  "c2-standard-16",  16,  64.0,   0.835200),
    ("c2-standard-30",  "c2-standard-30",  30,  120.0,  1.566000),
    ("c2-standard-60",  "c2-standard-60",  60,  240.0,  3.132000),
]
# Rough region markup on seeded us-central1 prices. Prefer the live API (Step 1)
# for accurate per-region pricing. These are approximations for display.
region_markup = {
    "us-central1":     1.00,
    "us-east1":        1.00,
    "us-west1":        1.00,
    "europe-west1":    1.10,
    "europe-west4":    1.10,
    "asia-south1":     1.16,   # Mumbai
    "asia-south2":     1.16,   # Delhi
    "asia-east1":      1.12,
    "asia-southeast1": 1.14,
}
markup = region_markup.get(region, 1.00)
if markup != 1.00:
    print(f"  Applying ~{markup}x markup on seeded us-central1 prices (approx; use live API for accuracy)")

if not live_fetched:
    for (ntid, mtype, vcpus, mem, price) in seed_prices:
        live_rows.append(
            Row(
                node_type_id=ntid,
                gcp_machine_type=mtype,
                vcpus=int(vcpus),
                memory_gb=float(mem),
                hourly_price_usd=round(float(price) * markup, 6),
                region=region,
                price_source=(
                    f"GCP list price (seeded, {region})" if markup == 1.0
                    else f"GCP list price (seeded us-central1, ~{markup}x markup for {region})"
                ),
            )
        )
    print(f"  Seeded {len(seed_prices)} known GCP VM types for {region}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: MERGE into gcp_vm_reference_prices

# COMMAND ----------

df = spark.createDataFrame(live_rows).withColumn("last_updated", F.current_timestamp())
df.createOrReplaceTempView("new_vm_prices")

spark.sql(f"""
    MERGE INTO {fq_schema}.gcp_vm_reference_prices AS target
    USING new_vm_prices AS source
    ON target.node_type_id = source.node_type_id AND target.region = source.region
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")

print(f"MERGE complete for {df.count()} rows in region {region}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Fallback estimates for unknown node types used in the workspace

# COMMAND ----------

try:
    spark.sql(f"""
        INSERT INTO {fq_schema}.gcp_vm_reference_prices
        SELECT DISTINCT
            nt.node_type                                                        AS node_type_id,
            nt.node_type                                                        AS gcp_machine_type,
            CAST(nt.core_count AS INT)                                          AS vcpus,
            ROUND(nt.memory_mb / 1024.0, 1)                                    AS memory_gb,
            ROUND(nt.core_count * 0.04749 + (nt.memory_mb / 1024.0) * 0.006383, 6) AS hourly_price_usd,
            '{region}'                                                          AS region,
            'Estimated from GCP N1 component pricing'                           AS price_source,
            current_timestamp()                                                 AS last_updated
        FROM system.compute.node_types nt
        LEFT JOIN {fq_schema}.gcp_vm_reference_prices vp
            ON nt.node_type = vp.node_type_id AND vp.region = '{region}'
        WHERE vp.node_type_id IS NULL
    """)
    est_count = spark.sql(f"""
        SELECT COUNT(*) AS cnt FROM {fq_schema}.gcp_vm_reference_prices
        WHERE price_source LIKE 'Estimated%' AND region = '{region}'
    """).collect()[0].cnt
    print(f"Estimated-price rows for unknown node types: {est_count}")
except Exception as e:
    print(f"  system.compute.node_types not available: {e}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary — times shown in IST (Asia/Kolkata)

# COMMAND ----------

total = spark.sql(f"""
    SELECT COUNT(*) AS cnt FROM {fq_schema}.gcp_vm_reference_prices WHERE region = '{region}'
""").collect()[0].cnt
by_source = spark.sql(f"""
    SELECT price_source, COUNT(*) AS cnt
    FROM {fq_schema}.gcp_vm_reference_prices
    WHERE region = '{region}'
    GROUP BY price_source
""").collect()
now_ist = spark.sql(
    "SELECT date_format(from_utc_timestamp(current_timestamp(), 'Asia/Kolkata'), 'yyyy-MM-dd HH:mm:ss') AS t"
).collect()[0].t

print(f"\n[{now_ist} IST] VM price refresh complete for region={region}")
print(f"  Total rows: {total}")
for r in by_source:
    print(f"    {r.price_source}: {r.cnt}")
