#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE_ROOT="${1:-/release}"
AGENT_ROOT="$RELEASE_ROOT/agent"

python -m pip install -q -r "$AGENT_ROOT/requirements-agent.txt"

cd "$AGENT_ROOT"
python - <<'PY'
from agent.engine_verifier import verify_official_engine

info = verify_official_engine(".")
import app.executor as executor

assert info["verified"] is True
assert callable(executor.run_command)
assert executor.__file__.endswith(".so")
assert len(info["artifacts"]) == 2
print(info)
print(f"Compiled import: {executor.__file__}")
PY

cp -a "$RELEASE_ROOT" /tmp/tampered-release
printf X >> /tmp/tampered-release/agent/app/executor*.so

cd /tmp/tampered-release/agent
if python - <<'PY' >/tmp/tamper-result.txt 2>&1
from agent.engine_verifier import verify_official_engine
verify_official_engine(".")
PY
then
  echo "Tampered engine was incorrectly accepted." >&2
  exit 1
fi

grep -q "checksum does not match" /tmp/tamper-result.txt
echo "Tamper detection OK"
