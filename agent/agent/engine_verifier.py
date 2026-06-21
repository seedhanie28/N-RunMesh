import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization


class EngineVerificationError(RuntimeError):
    pass


OFFICIAL_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAX2C0nis1lRDMIttm0sJ0CfK0p7D2IJ/BSMzyhOsFckI=
-----END PUBLIC KEY-----
"""


def verify_official_engine(base_dir):
    base = Path(base_dir).resolve()
    manifest_path = base / "engine-manifest.json"
    signature_path = base / "engine-manifest.sig"
    if not manifest_path.exists():
        if (
            os.getenv("NRUNMESH_ALLOW_SOURCE_ENGINE") == "1"
            and (base / "app" / "scheduler.py").exists()
        ):
            return {"mode": "development-source", "verified": False}
        raise EngineVerificationError("engine-manifest.json is missing")

    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    verified_artifacts = {}
    for artifact in manifest.get("artifacts", []):
        artifact_path = (base / artifact["file"]).resolve()
        if not artifact_path.is_relative_to(base):
            raise EngineVerificationError("engine path escapes the installation")
        if not artifact_path.exists():
            raise EngineVerificationError(f"compiled artifact is missing: {artifact['file']}")
        actual_digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if actual_digest != artifact.get("sha256"):
            raise EngineVerificationError(
                f"compiled artifact checksum does not match: {artifact['file']}"
            )
        verified_artifacts[artifact["file"]] = actual_digest

    if not verified_artifacts:
        raise EngineVerificationError("manifest contains no signed artifacts")

    public_key = serialization.load_pem_public_key(OFFICIAL_PUBLIC_KEY)
    signature = base64.b64decode(signature_path.read_text(encoding="ascii"))
    try:
        public_key.verify(signature, manifest_bytes)
    except InvalidSignature as exc:
        raise EngineVerificationError("engine signature is invalid") from exc

    return {
        "mode": "official-compiled",
        "verified": True,
        "version": manifest.get("version"),
        "platform": manifest.get("platform"),
        "artifacts": verified_artifacts,
    }
