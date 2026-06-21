#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
EXAMPLE_FILE="$REPO_ROOT/.env.example"

random_hex() {
  local bytes="${1:-24}"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$bytes"
  else
    python3 - "$bytes" <<'PY'
import secrets
import sys
print(secrets.token_hex(int(sys.argv[1])))
PY
  fi
}

replace_value() {
  local key="$1"
  local value="$2"
  local temp
  temp="$(mktemp)"
  awk -v key="$key" -v value="$value" '
    index($0, key "=") == 1 { print key "=" value; next }
    { print }
  ' "$ENV_FILE" > "$temp"
  mv "$temp" "$ENV_FILE"
}

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$EXAMPLE_FILE" "$ENV_FILE"

  DB_PASSWORD="$(random_hex 24)"
  SECRET_KEY="$(random_hex 32)"
  AGENT_KEY="$(random_hex 32)"
  ADMIN_PASSWORD="$(random_hex 12)"

  replace_value POSTGRES_PASSWORD "$DB_PASSWORD"
  replace_value SECRET_KEY "$SECRET_KEY"
  replace_value CRON_AGENT_API_KEY "$AGENT_KEY"
  replace_value NRUNMESH_ADMIN_PASSWORD "$ADMIN_PASSWORD"
  chmod 600 "$ENV_FILE"

  echo "Generated a new .env file."
  echo "Initial login: admin / $ADMIN_PASSWORD"
  echo "Save this password now. It is not printed on later starts."
fi

cd "$REPO_ROOT"
BACKUP_HOST_PATH="$(grep -E '^BACKUP_HOST_PATH=' "$ENV_FILE" | tail -n1 | cut -d= -f2- || true)"
BACKUP_HOST_PATH="${BACKUP_HOST_PATH:-./backups}"
if [[ "$BACKUP_HOST_PATH" != /* ]]; then
  BACKUP_HOST_PATH="$REPO_ROOT/${BACKUP_HOST_PATH#./}"
fi
mkdir -p "$BACKUP_HOST_PATH"
docker compose up -d --build
docker compose ps
