#!/usr/bin/env bash
# =============================================================================
# GCP Cost Observability — one-command installer
# =============================================================================
# Two ways to invoke:
#
# A. Remote (pipe):
#    curl -fsSL https://raw.githubusercontent.com/anujlathiisb/gcp-observability-databricks/main/install.sh \
#      | bash -s -- --workspace-host=https://... --profile=<p> --catalog=<c> \
#                   --region=us-central1 --emails=ops@example.com
#
# B. Local (clone first, then run):
#    git clone <repo>    # or unzip the solution folder
#    cd <folder>
#    ./install.sh --workspace-host=https://... --profile=<p> --catalog=<c> \
#                 --region=us-central1 --emails=ops@example.com
#
# Any flag can be omitted — the installer will prompt interactively.
# Flags can also be set via environment variables (see FLAGS section below).
# =============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/anujlathiisb/gcp-observability-databricks}"
REPO_BRANCH="${REPO_BRANCH:-main}"

# ----- Defaults (overridable via --flag=value or env var) --------------------
TARGET="${TARGET:-prod}"
WORKSPACE_HOST="${WORKSPACE_HOST:-}"
PROFILE="${PROFILE:-}"
CATALOG="${CATALOG:-}"
SCHEMA="${SCHEMA:-observability}"
CLOUD="${CLOUD:-GCP}"
REGION="${REGION:-us-central1}"
THRESHOLD="${THRESHOLD:-1000}"
TIERS="${TIERS:-25,50,75,90,100}"
EMAILS="${EMAILS:-}"
TZ_NAME="${TZ_NAME:-Asia/Kolkata}"
ETL_CRON="${ETL_CRON:-0 0 6 * * ?}"
ALERT_CRON="${ALERT_CRON:-0 0 * * * ?}"
GCP_SECRET_SCOPE="${GCP_SECRET_SCOPE:-}"
GCP_SECRET_NAME="${GCP_SECRET_NAME:-}"
RUN_ETL="${RUN_ETL:-true}"
NO_PROMPT="${NO_PROMPT:-}"

# ----- CLI flag parsing ------------------------------------------------------
for raw_arg in "$@"; do
  # Strip leading/trailing whitespace (handles paste artefacts from '\ ' line
  # continuations getting flattened into a single line in some terminals).
  arg="$(printf '%s' "$raw_arg" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  [ -z "$arg" ] && continue
  case "$arg" in
    --workspace-host=*)    WORKSPACE_HOST="${arg#*=}" ;;
    --profile=*)           PROFILE="${arg#*=}" ;;
    --catalog=*)           CATALOG="${arg#*=}" ;;
    --schema=*)            SCHEMA="${arg#*=}" ;;
    --cloud=*)             CLOUD="${arg#*=}" ;;
    --region=*)            REGION="${arg#*=}" ;;
    --threshold=*)         THRESHOLD="${arg#*=}" ;;
    --tiers=*)             TIERS="${arg#*=}" ;;
    --emails=*)            EMAILS="${arg#*=}" ;;
    --tz=*)                TZ_NAME="${arg#*=}" ;;
    --etl-cron=*)          ETL_CRON="${arg#*=}" ;;
    --alert-cron=*)        ALERT_CRON="${arg#*=}" ;;
    --gcp-secret-scope=*)  GCP_SECRET_SCOPE="${arg#*=}" ;;
    --gcp-secret-name=*)   GCP_SECRET_NAME="${arg#*=}" ;;
    --target=*)            TARGET="${arg#*=}" ;;
    --repo-url=*)          REPO_URL="${arg#*=}" ;;
    --branch=*)            REPO_BRANCH="${arg#*=}" ;;
    --no-run-etl)          RUN_ETL="false" ;;
    --yes|--non-interactive) NO_PROMPT=1 ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown flag: $arg" >&2; exit 2 ;;
  esac
done

log()  { printf "\033[1;34m[install]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m %s\n" "$*"; }
die()  { printf "\033[1;31m[error]\033[0m %s\n" "$*" >&2; exit 1; }

# ----- Determine working directory -------------------------------------------
# If running from curl | bash, we need to clone the repo somewhere.
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
fi

if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/scripts/deploy.py" ]; then
  PROJECT_DIR="$SCRIPT_DIR"
  log "Using local project at $PROJECT_DIR"
else
  PROJECT_DIR="${PWD}/gcp-cost-observability"
  if [ -d "$PROJECT_DIR/.git" ] || [ -f "$PROJECT_DIR/scripts/deploy.py" ]; then
    log "Reusing existing clone at $PROJECT_DIR"
  else
    command -v git >/dev/null 2>&1 || die "git is required"
    log "Cloning $REPO_URL ($REPO_BRANCH) -> $PROJECT_DIR"
    git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$PROJECT_DIR"
  fi
fi
cd "$PROJECT_DIR"

# ----- Check tools -----------------------------------------------------------
command -v python3 >/dev/null 2>&1 || die "python3 is required"

if ! python3 -c "import yaml" 2>/dev/null; then
  log "Installing PyYAML (user-local)"
  python3 -m pip install --user --quiet --break-system-packages pyyaml \
    || python3 -m pip install --user --quiet pyyaml \
    || die "Failed to install PyYAML. Try: pip3 install pyyaml"
fi

if ! command -v databricks >/dev/null 2>&1; then
  log "Installing Databricks CLI"
  if command -v brew >/dev/null 2>&1; then
    brew tap databricks/tap >/dev/null 2>&1 || true
    brew install databricks >/dev/null 2>&1 || die "brew install databricks failed"
  else
    curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh
  fi
fi

# ----- Collect missing values interactively ----------------------------------
prompt_if_empty() {
  local varname="$1" label="$2" default="${3:-}"
  local current="${!varname}"
  if [ -z "$current" ]; then
    if [ -n "$NO_PROMPT" ]; then die "$label is required (--${label,,//_/-}=... or env)"; fi
    if [ -n "$default" ]; then
      read -r -p "$label [$default]: " value
      value="${value:-$default}"
    else
      read -r -p "$label: " value
    fi
    eval "$varname=\"\$value\""
  fi
}

prompt_if_empty WORKSPACE_HOST "Workspace host (https://...)"
prompt_if_empty PROFILE        "Databricks CLI profile name"
prompt_if_empty CATALOG        "Unity Catalog catalog (must exist)"
prompt_if_empty EMAILS         "Alert recipient emails (comma-separated)"

# Normalise the workspace host to scheme://authority — strip any /path or ?query
# (users often copy the URL from their address bar which includes /browse?o=...)
if [[ "$WORKSPACE_HOST" =~ ^(https?://[^/?#]+) ]]; then
  _CLEAN_HOST="${BASH_REMATCH[1]}"
  if [ "$_CLEAN_HOST" != "$WORKSPACE_HOST" ]; then
    warn "Trimming workspace-host to '$_CLEAN_HOST' (was '$WORKSPACE_HOST')"
    WORKSPACE_HOST="$_CLEAN_HOST"
  fi
else
  die "workspace-host must start with http(s)://"
fi

# ----- Ensure the profile exists and is valid --------------------------------
if ! databricks auth profiles | awk 'NR>1{print $1}' | grep -qx "$PROFILE"; then
  log "Profile '$PROFILE' not found — running 'databricks auth login'"
  databricks auth login --host "$WORKSPACE_HOST" --profile "$PROFILE"
fi

# ----- Normalise emails ------------------------------------------------------
EMAILS_CSV="$(echo "$EMAILS" | tr -d '[:space:]')"
EMAILS_YAML=""
IFS=',' read -r -a _EMAIL_ARR <<< "$EMAILS_CSV"
for e in "${_EMAIL_ARR[@]}"; do
  [ -z "$e" ] && continue
  EMAILS_YAML+="  - ${e}\n"
done

# ----- Generate config.yml ---------------------------------------------------
log "Writing config.yml"
cat > config.yml <<EOF
workspace_host: "$WORKSPACE_HOST"
databricks_profile: "$PROFILE"

catalog: "$CATALOG"
schema:  "$SCHEMA"

cloud:  "$CLOUD"
region: "$REGION"

daily_threshold_dollars: $THRESHOLD
threshold_percentages:   "$TIERS"
alert_recipient_emails:
$(printf "$EMAILS_YAML")

schedule_timezone:   "$TZ_NAME"
etl_schedule_cron:   "$ETL_CRON"
alert_schedule_cron: "$ALERT_CRON"

gcp_billing_api_key_secret_scope: "$GCP_SECRET_SCOPE"
gcp_billing_api_key_secret_name:  "$GCP_SECRET_NAME"
EOF

log "config.yml ready:"
sed 's/^/    /' config.yml

# ----- Run the deploy --------------------------------------------------------
DEPLOY_ARGS=(--target="$TARGET")
[ "$RUN_ETL" = "true" ] && DEPLOY_ARGS+=(--run-etl)

log "Deploying..."
python3 ./scripts/deploy.py "${DEPLOY_ARGS[@]}"

log "Done. Open the dashboard:"
log "  databricks bundle open cost_observability_dashboard --target=$TARGET --profile=$PROFILE"
