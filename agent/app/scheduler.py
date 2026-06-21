import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional

# Python 3.9+ has zoneinfo; Python 3.8 uses backports
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8
    from backports.zoneinfo import ZoneInfo

from croniter import croniter, CroniterBadCronError
from flask import current_app

from .extensions import db
from .models import Job, Run, LogHistory, AppSetting
from app.utils.timezone import utcnow, to_local

import signal
import time
import tempfile

# How close to the scheduled cron time we still consider "due".
# If you call tick() every 10-30 seconds, 60 seconds is a safe window.
DUE_WINDOW_SECONDS = int(os.environ.get("CRON_DUE_WINDOW_SECONDS", "60"))

LOG_TAIL_LINES = int(os.environ.get("CRON_LOG_TAIL_LINES", "2000"))

RETENTION_DAYS = int(os.environ.get("CRON_LOG_RETENTION_DAYS", "7"))
_CLEANUP_EVERY_SECONDS = int(os.environ.get("CRON_CLEANUP_EVERY_SECONDS", "3600"))
_last_cleanup_ts = 0.0

# Kill runaway jobs (default 1 day). 0 disables.
MAX_RUN_SECONDS = int(os.environ.get("CRON_MAX_RUN_SECONDS", "86400"))
TERM_GRACE_SECONDS = int(os.environ.get("CRON_TERM_GRACE_SECONDS", "30"))

# Resource guards (0 disables)
# Example:
#   CRON_LOAD1_MAX=8
#   CRON_MEM_FREE_MIN_MB=1024
LOAD1_MAX = float(os.environ.get("CRON_LOAD1_MAX", "0"))
MEM_FREE_MIN_MB = int(os.environ.get("CRON_MEM_FREE_MIN_MB", "0"))

# Cron timezone: if set, cron is evaluated in this timezone (local schedule)
CRON_TIMEZONE = (os.environ.get("CRON_TIMEZONE") or os.environ.get("APP_TIMEZONE") or "UTC").strip()

def _configured_cron_timezone() -> str:
    try:
        row = db.session.get(AppSetting, "timezone")
        if row and row.value:
            return row.value
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
    return CRON_TIMEZONE

def _cron_now_naive_local() -> datetime:
    """Return 'now' as naive datetime in CRON_TIMEZONE for croniter evaluation."""
    tz = ZoneInfo(_configured_cron_timezone())
    return datetime.now(tz).replace(tzinfo=None)

def _local_naive_to_utc_naive(dt_local_naive: datetime) -> datetime:
    """Convert naive local(CRON_TIMEZONE) datetime -> naive UTC datetime (for DB comparisons)."""
    tz = ZoneInfo(_configured_cron_timezone())
    aware_local = dt_local_naive.replace(tzinfo=tz)
    aware_utc = aware_local.astimezone(timezone.utc)
    return aware_utc.replace(tzinfo=None)


def _mem_available_mb_linux() -> int:
    """Best-effort available memory in MB (Linux). Returns -1 if unknown."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
        # Prefer MemAvailable
        for line in txt.splitlines():
            if line.startswith("MemAvailable:"):
                kb = int(line.split()[1])
                return kb // 1024
        # Fallback: MemFree + Buffers + Cached
        memfree = buffers = cached = 0
        for line in txt.splitlines():
            if line.startswith("MemFree:"):
                memfree = int(line.split()[1])
            elif line.startswith("Buffers:"):
                buffers = int(line.split()[1])
            elif line.startswith("Cached:"):
                cached = int(line.split()[1])
        kb = memfree + buffers + cached
        return kb // 1024
    except Exception:
        return -1


def _resource_ok() -> Tuple[bool, str]:
    """Return (ok?, reason). If not ok, scheduler should skip spawning new runs."""
    try:
        if LOAD1_MAX and LOAD1_MAX > 0:
            l1 = os.getloadavg()[0]
            if l1 > LOAD1_MAX:
                return False, f"load1 {l1:.2f} > {LOAD1_MAX:.2f}"
    except Exception:
        pass

    if MEM_FREE_MIN_MB and MEM_FREE_MIN_MB > 0:
        ma = _mem_available_mb_linux()
        if ma >= 0 and ma < MEM_FREE_MIN_MB:
            return False, f"mem_available {ma}MB < {MEM_FREE_MIN_MB}MB"

    return True, "ok"


def _prepare_command(cmd: str) -> str:
    """Normalize command before execution.

    If commands were created on a different server and hardcode a python path,
    set env CRON_PYTHON_BIN on each server to rewrite at runtime.

    Example:
      CRON_PYTHON_BIN=/usr/bin/python3
    """
    cmd = (cmd or "").strip()
    pybin = (os.environ.get("CRON_PYTHON_BIN") or "").strip()
    if pybin:
        for old in ("/data/conda_dir/bin/python", "/data/conda_dir/bin/python3"):
            cmd = cmd.replace(old, pybin)
    return cmd


def _tail_file(path: str, max_lines: int = 2000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:])
    except Exception:
        return ""


def _kill_tree(pid: int, sig_: int) -> None:
    """Send signal to the process group (preferred), fallback to single PID."""
    if os.name == "nt":
        command = ["taskkill", "/PID", str(int(pid)), "/T"]
        if sig_ in (getattr(signal, "SIGKILL", None), signal.SIGTERM):
            command.append("/F")
        try:
            subprocess.run(command, check=False, capture_output=True)
        except Exception:
            pass
        return

    try:
        os.killpg(int(pid), sig_)
        return
    except Exception:
        pass
    try:
        os.kill(int(pid), sig_)
    except Exception:
        pass


def _persist_log_history_for_run(run_id: int) -> None:
    """UPSERT log_history from run.log_path so web can read logs cross-server."""
    run = db.session.get(Run, run_id)
    if not run:
        return

    h = LogHistory.query.filter_by(run_id=run.id).first()
    if h is None:
        h = LogHistory(run_id=run.id, job_id=run.job_id)
        db.session.add(h)

    h.job_id = run.job_id
    h.status = run.status or h.status or ""
    h.start_time = getattr(run, "start_time", None)
    h.end_time = getattr(run, "end_time", None)

    content = ""
    if run.log_path:
        content = _tail_file(run.log_path, LOG_TAIL_LINES)
    h.log_text = content

    db.session.commit()


def _cleanup_old_runs_and_logs(now: datetime) -> None:
    """Delete run log files + DB history older than retention.

    - removes /tmp/job_<id>.log files for completed runs older than RETENTION_DAYS
    - deletes log_history rows older than RETENTION_DAYS (based on created_at)
    - optionally deletes Run rows that are completed and old (keeps RUNNING/QUEUED)

    Runs inside try/except so scheduler never crashes.
    """
    try:
        cutoff = now - timedelta(days=RETENTION_DAYS)

        # 1) delete old log files for finished runs
        old_runs = (
            Run.query
            .filter(Run.status.in_(["SUCCESS", "FAILED"]))
            .filter(Run.end_time.isnot(None))
            .filter(Run.end_time < cutoff)
            .all()
        )
        for r in old_runs:
            try:
                if r.log_path and os.path.exists(r.log_path):
                    os.remove(r.log_path)
            except Exception:
                pass

        # 2) delete old log_history
        try:
            LogHistory.query.filter(LogHistory.created_at < cutoff).delete(synchronize_session=False)
        except Exception:
            # if created_at not present for some reason, skip
            pass

        # 3) delete old completed runs (optional, but keeps table small)
        Run.query.filter(Run.status.in_(["SUCCESS", "FAILED"]))\
            .filter(Run.end_time.isnot(None))\
            .filter(Run.end_time < cutoff)\
            .delete(synchronize_session=False)

        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _utcnow_naive() -> datetime:
    """UTC time but stored as naive datetime (works well with typical DB columns)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _spawn_command(cmd: str, log_file):
    """Start a job using the native command shell on each operating system."""
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=log_file,
            shell=True,
            creationflags=creationflags,
        )

    return subprocess.Popen(
        ["/bin/bash", "-lc", cmd],
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )


def _is_due(job: Job, now: datetime) -> Tuple[bool, Optional[datetime]]:
    """Return (due?, scheduled_at) for this job at the current time."""
    try:
        itr = croniter(job.cron, now)
        scheduled_at = itr.get_prev(datetime)
    except (CroniterBadCronError, ValueError):
        return False, None
    # Due if we're within the window from the last scheduled time.
    if (now - scheduled_at).total_seconds() >= DUE_WINDOW_SECONDS:
        return False, scheduled_at

    return True, scheduled_at


def _already_triggered_for_window(job_id: int, scheduled_at_utc: datetime) -> bool:
    """Prevent multiple triggers within the same cron window."""
    window_end = scheduled_at_utc + timedelta(seconds=DUE_WINDOW_SECONDS)

    # If there is already a run created for this job within the same window, skip.
    existing = (
        Run.query
        .filter(Run.job_id == job_id)
        .filter(Run.start_time >= scheduled_at_utc)
        .filter(Run.start_time < window_end)
        .first()
    )
    return existing is not None


def _watch_process_and_finalize(app_obj, run_id: int, proc: subprocess.Popen):
    """Wait for process exit, then update Run status/end_time."""
    rc = proc.wait()

    def _finalize():
        run = db.session.get(Run, run_id)
        if not run:
            return
        run.status = "SUCCESS" if rc == 0 else "FAILED"
        run.end_time = _utcnow_naive()
        try:
            run.process_pid = None
        except Exception:
            pass
        db.session.commit()
        try:
            _persist_log_history_for_run(run.id)
        except Exception:
            # don't crash scheduler because of log persistence
            try:
                db.session.rollback()
            except Exception:
                pass

    # Ensure DB ops run with app context if available
    if app_obj is not None:
        with app_obj.app_context():
            _finalize()
    else:
        _finalize()


def tick(server_name: str = "default"):
    """One scheduler tick.

    IMPORTANT: This function does NOT run by itself.
    You must call it periodically (e.g., every 10-30 seconds) from:
    - a dedicated scheduler process/service (your agent), OR
    - an in-app background scheduler (be careful with multi-worker deployments).

    Note: pass `server_name` to only run jobs assigned to that server.
    """
    # DB times stay UTC-naive, but cron schedule is evaluated in local timezone
    now_utc = _utcnow_naive()
    now_local = _cron_now_naive_local()

    global _last_cleanup_ts
    try:
        import time as _time
        if (_time.time() - _last_cleanup_ts) >= _CLEANUP_EVERY_SECONDS:
            _cleanup_old_runs_and_logs(now_utc)
            _last_cleanup_ts = _time.time()
    except Exception:
        pass

    # Reaper: kill RUNNING jobs that exceed MAX_RUN_SECONDS (default 1 day)
    if MAX_RUN_SECONDS > 0:
        try:
            cutoff = now_utc - timedelta(seconds=MAX_RUN_SECONDS)
            long_running = (
                Run.query
                .join(Job, Job.id == Run.job_id)
                .filter(Job.server == server_name)
                .filter(Run.status == "RUNNING")
                .filter(Run.start_time.isnot(None))
                .filter(Run.start_time < cutoff)
                .all()
            )

            for r in long_running:
                pid = getattr(r, "process_pid", None)
                if not pid:
                    continue

                # write a note to log file
                try:
                    if r.log_path:
                        with open(r.log_path, "a", encoding="utf-8", errors="replace") as _lf:
                            _lf.write(f"\n[auto-kill] exceeded {MAX_RUN_SECONDS}s, sending SIGTERM...\n")
                except Exception:
                    pass

                _kill_tree(int(pid), signal.SIGTERM)
                time.sleep(max(0, TERM_GRACE_SECONDS))

                # if still alive, SIGKILL
                try:
                    os.kill(int(pid), 0)
                    alive = True
                except ProcessLookupError:
                    alive = False
                except Exception:
                    alive = True

                if alive:
                    try:
                        if r.log_path:
                            with open(r.log_path, "a", encoding="utf-8", errors="replace") as _lf:
                                _lf.write("[auto-kill] still running, sending SIGKILL...\n")
                    except Exception:
                        pass
                    _kill_tree(int(pid), signal.SIGKILL)

                r.status = "FAILED"
                r.end_time = _utcnow_naive()
                try:
                    r.process_pid = None
                except Exception:
                    pass
                db.session.commit()

                try:
                    _persist_log_history_for_run(r.id)
                except Exception:
                    try:
                        db.session.rollback()
                    except Exception:
                        pass

        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass

    # capture real app object if we're running inside Flask context
    try:
        app_obj = current_app._get_current_object()
    except Exception:
        app_obj = None

    ok, reason = _resource_ok()
    if not ok:
        # server busy; don't start any new jobs this tick
        return

    # 0) Process manual QUEUED runs first (created by web/http agent)
    queued = (
        Run.query
        .join(Job, Job.id == Run.job_id)
        .filter(Run.status == "QUEUED")
        .filter(Job.server == server_name)
        .order_by(Run.id.asc())
        .limit(5)
        .all()
    )

    for r in queued:
        jobq = db.session.get(Job, r.job_id)
        if not jobq:
            r.status = "FAILED"
            r.end_time = _utcnow_naive()
            db.session.commit()
            continue

        # Respect max_running
        running = Run.query.filter_by(job_id=jobq.id, status="RUNNING").count()
        if running >= int(jobq.max_running or 1):
            continue

        r.status = "RUNNING"
        r.start_time = _utcnow_naive()
        db.session.commit()

        ts = to_local(utcnow()).strftime("%d%m%Y_%H%M%S")
        logfile = os.path.join(tempfile.gettempdir(), f"nrunmesh_job_{ts}_{r.id}.log")
        r.log_path = logfile
        db.session.commit()

        try:
            f = open(logfile, "w", encoding="utf-8", errors="replace")
        except TypeError:
            f = open(logfile, "w")

        try:
            cmd = _prepare_command(jobq.command)
            proc = _spawn_command(cmd, f)
            try:
                r.process_pid = int(proc.pid)
                db.session.commit()
            except Exception:
                pass
        except Exception as e:
            r.status = "FAILED"
            r.end_time = _utcnow_naive()
            db.session.commit()
            try:
                f.write(f"\n[spawn failed] {e!r}\n")
            except Exception:
                pass
            try:
                f.close()
            except Exception:
                pass
            continue

        t = threading.Thread(target=_watch_process_and_finalize, args=(app_obj, r.id, proc), daemon=True)
        t.start()

        try:
            f.close()
        except Exception:
            pass

    # Only jobs for this server
    jobs = Job.query.filter_by(is_active=True, server=server_name).all()

    for job in jobs:
        due, scheduled_at_local = _is_due(job, now_local)
        if not due or scheduled_at_local is None:
            continue

        scheduled_at_utc = _local_naive_to_utc_naive(scheduled_at_local)

        # Prevent multiple triggers for the same cron window
        if _already_triggered_for_window(job.id, scheduled_at_utc):
            continue

        # Respect max_running
        running = Run.query.filter_by(job_id=job.id, status="RUNNING").count()
        if running >= int(job.max_running or 1):
            continue

        # AUTO cron: enqueue first so UI can show QUEUED; the QUEUED worker above will start it.
        run = Run(job_id=job.id, status="QUEUED")
        try:
            run.triggered_by = "system"
        except Exception:
            pass

        # set start_time as scheduled_at_utc so window de-dup works even while QUEUED
        run.start_time = scheduled_at_utc

        db.session.add(run)
        db.session.commit()
