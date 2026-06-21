import argparse
import json
import logging
import os
import platform
import socket
import sys
import time
from pathlib import Path

import requests


BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from agent.engine_verifier import EngineVerificationError, verify_official_engine

try:
    ENGINE_INFO = verify_official_engine(BASE_DIR)
except EngineVerificationError as exc:
    raise SystemExit(f"N-RunMesh engine verification failed: {exc}") from exc

from app.executor import run_command


VERSION = "0.2.0"


def setup_logging():
    logging.basicConfig(
        level=getattr(
            logging,
            os.getenv("NRUNMESH_LOG_LEVEL", "INFO").upper(),
            logging.INFO,
        ),
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def normalize_url(value):
    return value.strip().rstrip("/")


def platform_name():
    return f"{platform.system()} {platform.release()} ({platform.machine()})"


def api_request(method, url, token=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.request(
        method,
        url,
        headers=headers,
        timeout=kwargs.pop("timeout", 30),
        **kwargs,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": response.text}
    if response.status_code >= 400:
        raise RuntimeError(
            payload.get("error") or f"Controller returned HTTP {response.status_code}"
        )
    return payload


def register_agent(args):
    controller_url = normalize_url(args.controller_url)
    payload = api_request(
        "POST",
        f"{controller_url}/api/v1/agents/register",
        json={
            "registration_token": args.registration_token,
            "agent_name": args.agent_name,
            "hostname": socket.gethostname(),
            "platform": platform_name(),
            "version": VERSION,
            "pid": os.getpid(),
        },
    )
    config = {
        "controller_url": controller_url,
        "agent_name": payload["agent_name"],
        "agent_token": payload["agent_token"],
        "poll_interval": payload.get("poll_interval", 10),
    }
    output = Path(args.config_file).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"Agent '{payload['agent_name']}' registered successfully.")
    print(f"Configuration saved to {output}")


def load_config(path):
    config_path = Path(path).resolve()
    if not config_path.is_file():
        raise SystemExit(
            f"Agent configuration not found: {config_path}. Run the installer again."
        )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for key in ("controller_url", "agent_name", "agent_token"):
        if not config.get(key):
            raise SystemExit(f"Agent configuration is missing '{key}'.")
    return config


def agent_payload():
    return {
        "hostname": socket.gethostname(),
        "platform": platform_name(),
        "version": VERSION,
        "pid": os.getpid(),
    }


def run_loop(config):
    controller = normalize_url(config["controller_url"])
    token = config["agent_token"]
    interval = max(3, int(config.get("poll_interval", 10)))
    logging.info(
        "N-RunMesh Agent started | name=%s | controller=%s | engine=%s",
        config["agent_name"],
        controller,
        ENGINE_INFO.get("mode"),
    )

    next_heartbeat = 0
    while True:
        try:
            now = time.monotonic()
            if now >= next_heartbeat:
                api_request(
                    "POST",
                    f"{controller}/api/v1/agents/heartbeat",
                    token=token,
                    json=agent_payload(),
                )
                next_heartbeat = now + 30

            payload = api_request(
                "POST",
                f"{controller}/api/v1/agents/work/claim",
                token=token,
                json=agent_payload(),
                timeout=35,
            )
            work = payload.get("work")
            if not work:
                time.sleep(interval)
                continue

            logging.info(
                "Running job | run_id=%s | job=%s",
                work["run_id"],
                work.get("name"),
            )
            result = run_command(
                work["command"],
                work["run_id"],
                timeout_seconds=work.get("max_seconds", 86400),
            )
            api_request(
                "POST",
                f"{controller}/api/v1/agents/runs/{work['run_id']}/complete",
                token=token,
                json=result,
                timeout=60,
            )
            logging.info(
                "Job completed | run_id=%s | status=%s | return_code=%s",
                work["run_id"],
                result["status"],
                result["return_code"],
            )
        except KeyboardInterrupt:
            logging.info("Agent stopped.")
            return
        except Exception as exc:
            logging.error("Controller communication failed: %s", exc)
            time.sleep(min(60, interval * 2))


def main():
    parser = argparse.ArgumentParser(description="N-RunMesh Agent")
    subparsers = parser.add_subparsers(dest="command")

    register = subparsers.add_parser("register")
    register.add_argument("--controller-url", required=True)
    register.add_argument("--registration-token", required=True)
    register.add_argument("--agent-name", required=True)
    register.add_argument("--config-file", required=True)

    run = subparsers.add_parser("run")
    run.add_argument(
        "--config-file",
        default=os.getenv("NRUNMESH_CONFIG", "agent.json"),
    )

    args = parser.parse_args()
    setup_logging()
    if args.command == "register":
        register_agent(args)
    elif args.command == "run":
        run_loop(load_config(args.config_file))
    else:
        parser.print_help()
        raise SystemExit(2)


if __name__ == "__main__":
    main()

