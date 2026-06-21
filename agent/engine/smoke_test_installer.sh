#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE_ROOT="${1:-/release}"
INSTALL_DIR="/tmp/nrunmesh-agent"
CONFIG_FILE="/tmp/nrunmesh-agent.env"
: "${NRUNMESH_TEST_CONTROLLER_URL:?Set NRUNMESH_TEST_CONTROLLER_URL}"
: "${NRUNMESH_TEST_SETUP_TOKEN:?Set NRUNMESH_TEST_SETUP_TOKEN}"

"$RELEASE_ROOT/deploy/linux/install.sh" \
  --mode manual \
  --name smoke-linux \
  --controller-url "$NRUNMESH_TEST_CONTROLLER_URL" \
  --setup-token "$NRUNMESH_TEST_SETUP_TOKEN" \
  --install-dir "$INSTALL_DIR" \
  --config-file "$CONFIG_FILE" \
  --non-interactive

test ! -f "$INSTALL_DIR/app/executor.py"
test -f "$INSTALL_DIR/engine-manifest.json"
test -f "$INSTALL_DIR/engine-manifest.sig"

echo "Installed API-only Agent package OK"
