import os
import subprocess
import threading
from datetime import datetime, timedelta
from app.utils.timezone import utcnow, to_local

from croniter import croniter, CroniterBadCronError
from flask import current_app

from .models import Job, Run, LogHistory
from .extensions import db

# How close to the scheduled cron time we still consider "due".
# If you call tick() every 10-30 seconds, 60 seconds is a safe window.
DUE_WINDOW_SECONDS = 60

# How many lines to keep when persisting logs into LogHistory
LOG_TAIL_LINES = 2000

def _tail_file(path: str, max_lines: int = LOG_TAIL_LINES) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:])
    except Exception as e:
        try:
            current_app.logger.exception("Failed tailing log file: %s", path)
        except Exception:
            pass
        return f"(failed to read log: {e!r})\n"


def _is_due(job: Job, now: datetime) -> tuple[bool, datetime | None]:
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


def _already_triggered_for_window(job_id: int, scheduled_at: datetime) -> bool:
    """Prevent multiple triggers within the same cron window."""
    window_end = scheduled_at + timedelta(seconds=DUE_WINDOW_SECONDS)

    # If your Run model has start_time (it does in the UI), this prevents duplicates.
    try:
        existing = (
            Run.query
            .filter(Run.job_id == job_id)
            .filter(Run.start_time >= scheduled_at)
            .filter(Run.start_time < window_end)
            .first()
        )
        return existing is not None
    except Exception:
        # Fallback: if start_time isn't available, at least avoid duplicate RUNNING in that minute.
        try:
            running = Run.query.filter_by(job_id=job_id, status="RUNNING").count()
            return running > 0
        except Exception:
            return False


def _persist_log_history_for_run(run_id: int):
    """Persist log text into LogHistory once a run is finished."""
    run = db.session.get(Run, run_id)
    if not run:
        return

    existing = LogHistory.query.filter_by(run_id=run.id).first()
    if existing:
        return

    content = ""
    if getattr(run, "log_path", None) and os.path.exists(run.log_path):
        content = _tail_file(run.log_path, LOG_TAIL_LINES)

    h = LogHistory(
        run_id=run.id,
        job_id=run.job_id,
        status=run.status or "",
        start_time=getattr(run, "start_time", None),
        end_time=getattr(run, "end_time", None),
        log_text=content,
    )
    db.session.add(h)
    db.session.commit()


def _watch_process_and_finalize(app, run_id: int, proc: subprocess.Popen):
    """Wait for process exit, then update Run status/end_time and persist LogHistory."""
    rc = proc.wait()
    def _finalize():
        try:
            run = db.session.get(Run, run_id)
            if not run:
                return

            run.status = "SUCCESS" if rc == 0 else "FAILED"
            try:
                run.end_time = utcnow()
            except Exception:
                pass

            db.session.commit()
            _persist_log_history_for_run(run.id)
        except Exception:
            try:
                current_app.logger.exception("Failed finalizing run_id=%s", run_id)
            except Exception:
                pass

    # Ensure DB ops run with app context if available
    if app is not None:
        with app.app_context():
            _finalize()
    else:
        _finalize()


def run_now(job: Job) -> Run:
    """Run a job immediately (ignore cron schedule)."""
    now = to_local(utcnow())

    # Respect max_running
    running = Run.query.filter_by(job_id=job.id, status="RUNNING").count()
    if running >= int(job.max_running or 1):
        raise RuntimeError("max_running reached")

    # capture real app object if we're running inside Flask context
    try:
        app_obj = current_app._get_current_object()
    except Exception:
        app_obj = None

    run = Run(job_id=job.id, status="RUNNING")
    try:
        run.start_time = now
    except Exception:
        pass

    db.session.add(run)
    db.session.commit()

    ts = utcnow().strftime("%d%m%Y_%H%M%S")
    logfile = f"/tmp/job_{ts}_{run.id}.log"
    run.log_path = logfile
    db.session.commit()

    # Start process and redirect stdout/stderr to logfile
    try:
        f = open(logfile, "w", encoding="utf-8", errors="replace")
    except TypeError:
        f = open(logfile, "w")

    try:
        proc = subprocess.Popen(
            ["/bin/bash", "-lc", job.command],
            stdout=f,
            stderr=f,
        )
        try:
            run.process_pid = int(proc.pid)
            db.session.commit()
        except Exception:
            pass
    except Exception as e:
        try:
            run.status = "FAILED"
            run.end_time = utcnow()
        except Exception:
            pass
        db.session.commit()
        try:
            f.write(f"\n[spawn failed] {e!r}\n")
        except Exception:
            pass
        try:
            f.close()
        except Exception:
            pass
        _persist_log_history_for_run(run.id)
        return run

    # Watch process in a background thread and finalize
    t = threading.Thread(
        target=_watch_process_and_finalize,
        args=(app_obj, run.id, proc),
        daemon=True,
    )
    t.start()

    try:
        f.close()
    except Exception:
        pass

    return run

def tick(server_name: str = "default"):
    """One scheduler tick.
    IMPORTANT: This function does NOT run by itself.
    You must call it periodically (e.g., every 10-30 seconds) from:
    - a dedicated scheduler process/service, OR
    - an in-app background scheduler (be careful with multi-worker deployments).
    """
    now = to_local(utcnow())
    jobs = Job.query.filter_by(is_active=True, server=server_name).all()
    # capture real app object if we're running inside Flask context
    try:
        app_obj = current_app._get_current_object()
    except Exception:
        app_obj = None

    for job in jobs:
        due, scheduled_at = _is_due(job, now)
        if not due or scheduled_at is None:
            continue
        # Prevent multiple triggers for the same cron window
        if _already_triggered_for_window(job.id, scheduled_at):
            continue
        # Respect max_running
        running = Run.query.filter_by(job_id=job.id, status="RUNNING").count()
        if running >= int(job.max_running or 1):
            continue
        run = Run(job_id=job.id, status="RUNNING",triggered_by="system")
        # Ensure UI timers have start_time
        try:
            run.start_time = now
        except Exception:
            pass

        db.session.add(run)
        db.session.commit()

        ts = utcnow().strftime("%d%m%Y_%H%M%S")
        logfile = f"/tmp/job_{ts}_{run.id}.log"
        run.log_path = logfile
        db.session.commit()
        # Start process and redirect stdout/stderr to logfile
        try:
            f = open(logfile, "w", encoding="utf-8", errors="replace")
        except TypeError:
            # For older python on some systems
            f = open(logfile, "w")

        try:
            proc = subprocess.Popen(
                ["/bin/bash", "-lc", job.command],
                stdout=f,
                stderr=f,
            )
            try:
                run.process_pid = int(proc.pid)
                db.session.commit()
            except Exception:
                pass
        except Exception as e:
            # Failed to spawn
            try:
                run.status = "FAILED"
                run.end_time = utcnow()
            except Exception:
                pass
            db.session.commit()
            f.write(f"\n[spawn failed] {e!r}\n")
            f.close()
            _persist_log_history_for_run(run.id)
            continue
        # Watch process in a background thread and finalize
        t = threading.Thread(
            target=_watch_process_and_finalize,
            args=(app_obj, run.id, proc),
            daemon=True,
        )
        t.start()
        # Parent can close its file handle; child keeps writing.
        try:
            f.close()
        except Exception:
            pass