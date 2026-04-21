#!/usr/bin/env python3
"""
Reads config.yml, renders the Lakeview dashboard, and writes local_overrides.yml
containing workspace.host AND every bundle variable for the chosen target.
Then runs `databricks bundle deploy`.

Usage:
    ./scripts/deploy.py --target=prod
    ./scripts/deploy.py --target=dev --skip-prepare
    ./scripts/deploy.py --target=prod --run-etl
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

try:
    import yaml  # PyYAML
except ImportError:
    sys.exit("PyYAML required. Install with: pip install pyyaml")


def run(*args: str) -> None:
    print(f"\n$ {' '.join(args)}")
    subprocess.run(list(args), check=True)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="prod", choices=["dev", "staging", "prod"])
    p.add_argument("--config", default="config.yml")
    p.add_argument("--skip-prepare", action="store_true")
    p.add_argument("--skip-deploy", action="store_true")
    p.add_argument("--run-etl", action="store_true", help="Run cost_pipeline after deploy")
    args = p.parse_args()

    root = Path(__file__).resolve().parent.parent
    config_path = root / args.config
    if not config_path.exists():
        sys.exit(f"Config not found: {config_path}")

    cfg = yaml.safe_load(config_path.read_text())

    required = ["workspace_host", "catalog", "schema", "cloud", "region",
                "daily_threshold_dollars", "alert_recipient_emails"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        sys.exit(f"config.yml missing required keys: {missing}")

    # --- 1. Write local_overrides.yml with host + all variables ---
    variables = {
        "catalog": str(cfg["catalog"]),
        "schema": str(cfg["schema"]),
        "cloud": str(cfg["cloud"]).upper(),
        "region": str(cfg["region"]),
        "daily_threshold_dollars": str(cfg["daily_threshold_dollars"]),
        "threshold_percentages": str(cfg.get("threshold_percentages", "25,50,75,90,100")),
        "alert_recipient_emails": ",".join(cfg["alert_recipient_emails"]),
        "alert_emails_list": list(cfg["alert_recipient_emails"]),
        "etl_schedule_cron": str(cfg.get("etl_schedule_cron", "0 0 6 * * ?")),
        "alert_schedule_cron": str(cfg.get("alert_schedule_cron", "0 0 * * * ?")),
        "schedule_timezone": str(cfg.get("schedule_timezone", "Asia/Kolkata")),
        "gcp_billing_api_key_secret_scope": str(cfg.get("gcp_billing_api_key_secret_scope", "")),
        "gcp_billing_api_key_secret_name": str(cfg.get("gcp_billing_api_key_secret_name", "")),
    }
    overrides = {
        "targets": {
            args.target: {
                "workspace": {"host": cfg["workspace_host"]},
                "variables": variables,
            }
        }
    }
    overrides_path = root / "local_overrides.yml"
    overrides_path.write_text(yaml.safe_dump(overrides, sort_keys=False))
    print(f"Wrote {overrides_path.name}")
    print(f"  host       : {cfg['workspace_host']}")
    print(f"  target     : {args.target}")
    print(f"  catalog.sch: {cfg['catalog']}.{cfg['schema']}")
    print(f"  cloud.reg  : {cfg['cloud']}/{cfg['region']}")
    print(f"  threshold  : ${cfg['daily_threshold_dollars']}/day, tiers {variables['threshold_percentages']}")
    print(f"  recipients : {cfg['alert_recipient_emails']}")
    print(f"  timezone   : {variables['schedule_timezone']}")

    # --- 2. Render the Lakeview dashboard template ---
    if not args.skip_prepare:
        run(str(root / "scripts/prepare.sh"),
            f"--catalog={cfg['catalog']}",
            f"--schema={cfg['schema']}")

    # --- 3. Deploy ---
    if not args.skip_deploy:
        profile = cfg.get("databricks_profile")
        profile_args = ["--profile", profile] if profile else []
        run("databricks", "bundle", "deploy",
            "--target", args.target,
            "--force", "--auto-approve",
            *profile_args)

    # --- 4. Optionally kick off the ETL ---
    if args.run_etl:
        profile_args = ["--profile", cfg["databricks_profile"]] if cfg.get("databricks_profile") else []
        run("databricks", "bundle", "run", "cost_pipeline",
            "--target", args.target, *profile_args)

    print(f"\n[deploy.py] Done. Target: {args.target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
