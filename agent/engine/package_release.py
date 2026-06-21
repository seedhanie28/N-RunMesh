import argparse
import base64
import hashlib
import json
import os
import platform
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent


def find_engine(target_platform):
    suffix = ".pyd" if target_platform.startswith("windows") else ".so"
    candidates = list(ROOT.glob(f"app/executor*{suffix}"))
    if len(candidates) != 1:
        raise SystemExit(
            f"Expected exactly one {suffix} executor binary in agent/app "
            f"for {target_platform}; found {len(candidates)}"
        )
    return candidates[0]


def copy_runtime(target, target_platform):
    agent_target = target / "agent"
    foreign_binary = "*.so" if target_platform.startswith("windows") else "*.pyd"
    shutil.copytree(
        ROOT / "app",
        agent_target / "app",
        ignore=shutil.ignore_patterns(
            "executor.py",
            "executor.c",
            "scheduler.py",
            "scheduler.c",
            "scheduler*.so",
            "scheduler*.pyd",
            foreign_binary,
            "models.py",
            "extensions.py",
            "config.py",
            "utils",
            "__pycache__",
            "*.pyc",
            "._*",
        ),
    )
    shutil.copytree(
        ROOT / "agent",
        agent_target / "agent",
        ignore=shutil.ignore_patterns(
            "engine_verifier.py",
            "engine_verifier.c",
            "__pycache__",
            "*.pyc",
            "._*",
            foreign_binary,
        ),
    )
    shutil.copy2(ROOT / "requirements-agent.txt", agent_target / "requirements-agent.txt")
    shutil.copy2(ROOT / "engine_public_key.pem", agent_target / "engine_public_key.pem")
    (target / "deploy" / "linux").mkdir(parents=True)
    (target / "deploy" / "windows").mkdir(parents=True)
    shutil.copy2(WORKSPACE / "deploy" / "linux" / "install.sh", target / "deploy" / "linux" / "install.sh")
    shutil.copy2(WORKSPACE / "deploy" / "windows" / "install.ps1", target / "deploy" / "windows" / "install.ps1")


def create_linux_self_extracting_installer(target, archive_path, version, platform_name):
    payload = Path(archive_path).read_bytes()
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    output = target.parent / f"nrunmesh-agent-{version}-{platform_name}.sh"
    header = f"""#!/usr/bin/env bash
set -Eeuo pipefail

PRODUCT="N-RunMesh Agent {version} ({platform_name})"
PAYLOAD_SHA256="{payload_sha256}"
MARKER="__NRUNMESH_PAYLOAD_BELOW__"

usage() {{
  cat <<'EOF'
{output.name}

Self-extracting N-RunMesh Agent installer.
All installer options are forwarded to the embedded installer.

Examples:
  sudo ./{output.name}
  sudo ./{output.name} --mode automatic --controller-url http://server:3012 --setup-token TOKEN

Use --installer-help to display all installation options.
EOF
}}

if [[ "${{1:-}}" == "--help" ]]; then
  usage
  exit 0
fi

payload_line="$(awk -v marker="$MARKER" '$0 == marker {{ print NR + 1; exit }}' "$0")"
if [[ -z "$payload_line" ]]; then
  echo "Embedded agent payload was not found." >&2
  exit 1
fi

work_dir="$(mktemp -d "${{TMPDIR:-/tmp}}/nrunmesh-agent.XXXXXX")"
cleanup() {{
  rm -rf -- "$work_dir"
}}
trap cleanup EXIT INT TERM

payload_file="$work_dir/payload.tar.gz"
tail -n +"$payload_line" "$0" > "$payload_file"

if command -v sha256sum >/dev/null 2>&1; then
  actual_sha256="$(sha256sum "$payload_file" | awk '{{print $1}}')"
elif command -v shasum >/dev/null 2>&1; then
  actual_sha256="$(shasum -a 256 "$payload_file" | awk '{{print $1}}')"
else
  echo "sha256sum or shasum is required to verify the installer." >&2
  exit 1
fi

if [[ "$actual_sha256" != "$PAYLOAD_SHA256" ]]; then
  echo "Installer payload checksum does not match. Download a fresh copy." >&2
  exit 1
fi

tar -xzf "$payload_file" -C "$work_dir"
release_root="$work_dir/nrunmesh-agent-{version}-{platform_name}"
installer="$release_root/deploy/linux/install.sh"
if [[ ! -f "$installer" ]]; then
  echo "Embedded Linux installer is missing." >&2
  exit 1
fi

if [[ "${{1:-}}" == "--installer-help" ]]; then
  shift
  bash "$installer" --help "$@"
  exit $?
fi

echo "$PRODUCT"
bash "$installer" "$@"
exit $?
__NRUNMESH_PAYLOAD_BELOW__
""".encode("utf-8")
    output.write_bytes(header + payload)
    output.chmod(0o755)
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--output", default=str(WORKSPACE / "dist"))
    args = parser.parse_args()

    engine = find_engine(args.platform)
    output_root = Path(args.output).resolve()
    target = output_root / f"nrunmesh-agent-{args.version}-{args.platform}"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    copy_runtime(target, args.platform)

    agent_target = target / "agent"
    expected_suffix = ".pyd" if args.platform.startswith("windows") else ".so"
    packaged_engine = next(
        (agent_target / "app").glob(f"executor*{expected_suffix}"),
        None,
    )
    if packaged_engine is None:
        raise SystemExit("Compiled engine was not copied to release package")

    verifier = next(
        (agent_target / "agent").glob(f"engine_verifier*{expected_suffix}"),
        None,
    )
    if verifier is None:
        raise SystemExit("Compiled engine verifier was not copied to release package")

    artifacts = []
    for artifact in (packaged_engine, verifier):
        artifacts.append({
            "file": artifact.relative_to(agent_target).as_posix(),
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        })

    manifest = {
        "product": "N-RunMesh Agent Execution Engine",
        "version": args.version,
        "platform": args.platform,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "artifacts": artifacts,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_bytes = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    private_key = serialization.load_pem_private_key(
        Path(args.private_key).read_bytes(),
        password=None,
    )
    signature = private_key.sign(manifest_bytes)

    (agent_target / "engine-manifest.json").write_bytes(manifest_bytes)
    (agent_target / "engine-manifest.sig").write_text(
        base64.b64encode(signature).decode("ascii"),
        encoding="ascii",
    )
    archive_format = "zip" if args.platform.startswith("windows") else "gztar"
    archive_path = shutil.make_archive(
        str(target),
        archive_format,
        root_dir=output_root,
        base_dir=target.name,
    )
    print(target)
    if args.platform.startswith("windows"):
        print(archive_path)
    else:
        installer_path = create_linux_self_extracting_installer(
            target,
            archive_path,
            args.version,
            args.platform,
        )
        Path(archive_path).unlink()
        print(installer_path)


if __name__ == "__main__":
    main()
