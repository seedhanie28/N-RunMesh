#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
SOURCE_DIR="$REPO_ROOT/agent"

MODE=""
AGENT_NAME="${HOSTNAME:-$(hostname)}"
CONTROLLER_URL=""
SETUP_TOKEN=""
INTERVAL="15"
SERVICE_USER="${SUDO_USER:-${USER:-$(id -un)}}"
INSTALL_DIR=""
CONFIG_FILE=""
NON_INTERACTIVE="0"
NAME_WAS_SET="0"

usage() {
  cat <<'EOF'
N-RunMesh Agent installer

Usage:
  ./install.sh [options]

Options:
  --mode manual|automatic
  --name AGENT_NAME
  --controller-url URL       N-RunMesh Controller URL
  --setup-token TOKEN        One-time token from Agents page
  --service-user USER
  --install-dir PATH
  --config-file PATH
  --non-interactive
  -h, --help

manual:
  Installs the agent and launcher. Start it yourself with nrunmesh-agent.

automatic:
  Installs a systemd service and starts it immediately. Run as root/sudo.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --name) AGENT_NAME="${2:-}"; NAME_WAS_SET="1"; shift 2 ;;
    --controller-url) CONTROLLER_URL="${2:-}"; shift 2 ;;
    --setup-token) SETUP_TOKEN="${2:-}"; shift 2 ;;
    --interval) INTERVAL="${2:-}"; shift 2 ;;
    --service-user) SERVICE_USER="${2:-}"; shift 2 ;;
    --install-dir) INSTALL_DIR="${2:-}"; shift 2 ;;
    --config-file) CONFIG_FILE="${2:-}"; shift 2 ;;
    --non-interactive) NON_INTERACTIVE="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ ! -d "$SOURCE_DIR/app" || ! -f "$SOURCE_DIR/agent/cron_agent.py" ]]; then
  echo "Agent source not found at: $SOURCE_DIR" >&2
  exit 1
fi
if [[ ! -f "$SOURCE_DIR/engine-manifest.json" || ! -f "$SOURCE_DIR/engine-manifest.sig" ]]; then
  echo "Official compiled engine manifest is missing." >&2
  echo "Install from a packaged N-RunMesh Agent release, not the source checkout." >&2
  exit 1
fi

if [[ -z "$MODE" ]]; then
  if [[ "$NON_INTERACTIVE" == "1" ]]; then
    echo "--mode is required with --non-interactive" >&2
    exit 2
  fi
  echo "Choose installation mode:"
  echo "  1) manual    - install only; you start the agent"
  echo "  2) automatic - install and run as a systemd service"
  read -r -p "Mode [1]: " choice
  case "${choice:-1}" in
    1|manual) MODE="manual" ;;
    2|automatic) MODE="automatic" ;;
    *) echo "Invalid mode." >&2; exit 2 ;;
  esac
fi

if [[ "$MODE" != "manual" && "$MODE" != "automatic" ]]; then
  echo "--mode must be manual or automatic" >&2
  exit 2
fi

if [[ "$MODE" == "automatic" && "$(id -u)" -ne 0 ]]; then
  echo "Automatic mode requires root. Run: sudo $0 --mode automatic ..." >&2
  exit 1
fi

if [[ -z "$CONTROLLER_URL" ]]; then
  if [[ "$NON_INTERACTIVE" == "1" ]]; then
    echo "--controller-url is required with --non-interactive" >&2
    exit 2
  fi
  read -r -p "Controller URL (example: https://runmesh.example.com): " CONTROLLER_URL
fi
if [[ -z "$CONTROLLER_URL" ]]; then
  echo "Controller URL is required." >&2
  exit 1
fi

if [[ "$NON_INTERACTIVE" != "1" && "$NAME_WAS_SET" != "1" ]]; then
  read -r -p "Agent name [$AGENT_NAME]: " input_name
  AGENT_NAME="${input_name:-$AGENT_NAME}"
fi

if [[ -z "$SETUP_TOKEN" && "$NON_INTERACTIVE" != "1" ]]; then
  read -r -s -p "One-time setup token: " SETUP_TOKEN
  echo
fi
if [[ -z "$SETUP_TOKEN" ]]; then
  echo "--setup-token is required." >&2
  exit 2
fi

if [[ -z "$INSTALL_DIR" ]]; then
  if [[ "$MODE" == "automatic" ]]; then
    INSTALL_DIR="/opt/nrunmesh-agent"
  else
    INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/nrunmesh-agent"
  fi
fi

if [[ -z "$CONFIG_FILE" ]]; then
  if [[ "$MODE" == "automatic" ]]; then
    CONFIG_FILE="/etc/nrunmesh/agent.env"
  else
    CONFIG_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/nrunmesh/agent.env"
  fi
fi

PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "$candidate")"
    break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python is not installed; installing it automatically..."
  runner=()
  if [[ "$(id -u)" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then runner=(sudo); else
      echo "Run this installer with sudo so Python can be installed." >&2
      exit 1
    fi
  fi
  if command -v apt-get >/dev/null 2>&1; then
    "${runner[@]}" apt-get update
    "${runner[@]}" apt-get install -y python3 python3-venv
  elif command -v dnf >/dev/null 2>&1; then
    "${runner[@]}" dnf install -y python3
  elif command -v yum >/dev/null 2>&1; then
    "${runner[@]}" yum install -y python3
  else
    echo "Automatic Python installation is not supported on this OS." >&2
    exit 1
  fi
  PYTHON_BIN="$(command -v python3)"
fi

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit("Python 3.9+ is required")
PY

escape_env() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '"%s"' "$value"
}

echo "Installing N-RunMesh Agent to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$(dirname "$CONFIG_FILE")"
rm -rf "$INSTALL_DIR/app" "$INSTALL_DIR/agent"
cp -R "$SOURCE_DIR/app" "$INSTALL_DIR/app"
cp -R "$SOURCE_DIR/agent" "$INSTALL_DIR/agent"
cp "$SOURCE_DIR/requirements-agent.txt" "$INSTALL_DIR/requirements.txt"
for artifact in engine-manifest.json engine-manifest.sig engine_public_key.pem; do
  if [[ -f "$SOURCE_DIR/$artifact" ]]; then
    cp "$SOURCE_DIR/$artifact" "$INSTALL_DIR/$artifact"
  fi
done
find "$INSTALL_DIR/app" "$INSTALL_DIR/agent" -name '._*' -type f -delete
find "$INSTALL_DIR/app" "$INSTALL_DIR/agent" -name '__pycache__' -type d -prune -exec rm -rf {} +

install_venv_support() {
  local runner=()
  if [[ "$(id -u)" -eq 0 ]]; then
    runner=()
  elif command -v sudo >/dev/null 2>&1; then
    runner=(sudo)
  else
    echo "Python venv support is missing and sudo is unavailable." >&2
    return 1
  fi

  if command -v apt-get >/dev/null 2>&1; then
    local version
    version="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    "${runner[@]}" apt-get update
    "${runner[@]}" apt-get install -y "python${version}-venv" || \
      "${runner[@]}" apt-get install -y python3-venv
  elif command -v dnf >/dev/null 2>&1; then
    "${runner[@]}" dnf install -y python3
  elif command -v yum >/dev/null 2>&1; then
    "${runner[@]}" yum install -y python3
  else
    echo "Install the Python venv package for this operating system, then rerun the installer." >&2
    return 1
  fi
}

rm -rf "$INSTALL_DIR/.venv"
if ! "$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"; then
  echo "Python venv support is missing; installing the OS package..."
  install_venv_support
  rm -rf "$INSTALL_DIR/.venv"
  "$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip
"$INSTALL_DIR/.venv/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"

"$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/agent/cron_agent.py" register \
  --controller-url "$CONTROLLER_URL" \
  --registration-token "$SETUP_TOKEN" \
  --agent-name "$AGENT_NAME" \
  --config-file "$CONFIG_FILE"
chmod 600 "$CONFIG_FILE"

mkdir -p "$INSTALL_DIR/bin"
cat > "$INSTALL_DIR/bin/nrunmesh-agent" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/agent/cron_agent.py" run --config-file "$CONFIG_FILE" "\$@"
EOF
chmod +x "$INSTALL_DIR/bin/nrunmesh-agent"

if [[ "$MODE" == "automatic" ]]; then
  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "Service user does not exist: $SERVICE_USER" >&2
    exit 1
  fi

  chown -R "$SERVICE_USER":"$(id -gn "$SERVICE_USER")" "$INSTALL_DIR"
  chown root:"$(id -gn "$SERVICE_USER")" "$CONFIG_FILE"
  chmod 640 "$CONFIG_FILE"

  cat > /etc/systemd/system/nrunmesh-agent.service <<EOF
[Unit]
Description=N-RunMesh Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/bin/nrunmesh-agent
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable --now nrunmesh-agent.service
  echo "Automatic installation complete."
  echo "Status: systemctl status nrunmesh-agent"
  echo "Logs:   journalctl -u nrunmesh-agent -f"
else
  echo "Manual installation complete."
  echo "Start: $INSTALL_DIR/bin/nrunmesh-agent"
  echo "Config: $CONFIG_FILE"
fi
