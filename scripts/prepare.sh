#!/usr/bin/env bash
# Render the Lakeview dashboard template into a deploy-ready file by
# substituting {{CATALOG_SCHEMA}} with the user-provided catalog.schema.
#
# Usage:
#   ./scripts/prepare.sh --catalog=<cat> --schema=<schema>
#   ./scripts/prepare.sh   # reads CATALOG/SCHEMA env vars, defaults main/observability
#
# Run this ONCE before `databricks bundle deploy` (and any time you change
# the catalog or schema). The generated file is gitignored.

set -euo pipefail

CATALOG="${CATALOG:-main}"
SCHEMA="${SCHEMA:-observability}"

for arg in "$@"; do
  case "$arg" in
    --catalog=*) CATALOG="${arg#*=}" ;;
    --schema=*)  SCHEMA="${arg#*=}"  ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
TEMPLATE="$PROJECT_ROOT/src/cost_observability_dashboard.lvdash.json.template"
OUTPUT="$PROJECT_ROOT/src/cost_observability_dashboard.lvdash.json"

if [ ! -f "$TEMPLATE" ]; then
  echo "Template not found: $TEMPLATE" >&2
  exit 1
fi

FQ="${CATALOG}.${SCHEMA}"
# Current month in IST, formatted to match ds_month_picker.month_label ("Apr 2026")
DEFAULT_MONTH_LABEL="$(TZ=Asia/Kolkata date '+%b %Y')"

sed -e "s/{{CATALOG_SCHEMA}}/${FQ}/g" \
    -e "s/{{DEFAULT_MONTH_LABEL}}/${DEFAULT_MONTH_LABEL}/g" \
    "$TEMPLATE" > "$OUTPUT"

echo "Rendered dashboard -> $OUTPUT"
echo "  catalog.schema: $FQ"
echo "  default month : $DEFAULT_MONTH_LABEL"
