import os
import signal
import subprocess
import tempfile
from pathlib import Path


def _kill_process_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            process.kill()


def run_command(command, run_id, timeout_seconds=86400, max_log_bytes=1048576):
    log_path = Path(tempfile.gettempdir()) / f"nrunmesh_run_{run_id}.log"
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        if os.name == "nt":
            process = subprocess.Popen(
                command,
                stdout=log_file,
                stderr=log_file,
                shell=True,
                creationflags=getattr(
                    subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    0,
                ),
            )
        else:
            process = subprocess.Popen(
                ["/bin/bash", "-lc", command],
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )

        timed_out = False
        try:
            return_code = process.wait(
                timeout=max(1, int(timeout_seconds or 86400))
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(process)
            return_code = -1
            log_file.write(
                f"\n[N-RunMesh] Job exceeded {timeout_seconds}s and was terminated.\n"
            )

    content = log_path.read_text(encoding="utf-8", errors="replace")
    encoded = content.encode("utf-8", errors="replace")
    if len(encoded) > max_log_bytes:
        content = encoded[-max_log_bytes:].decode("utf-8", errors="replace")
        content = "[N-RunMesh] Log truncated.\n" + content
    log_path.unlink(missing_ok=True)

    return {
        "status": "SUCCESS" if return_code == 0 and not timed_out else "FAILED",
        "return_code": return_code,
        "log": content,
    }

