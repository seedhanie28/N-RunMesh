import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from croniter import croniter, CroniterBadCronError
from flask import Blueprint, jsonify, request

from .extensions import db
from .models import (
    AgentRegistrationToken,
    AgentRegistry,
    AppSetting,
    Job,
    LogHistory,
    Run,
)


agent_api = Blueprint("agent_api", __name__, url_prefix="/api/v1/agents")


def token_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def agent_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify({"ok": False, "error": "missing agent credential"}), 401
        credential = header[7:].strip()
        agent = AgentRegistry.query.filter_by(
            token_hash=token_hash(credential)
        ).first()
        if agent is None:
            return jsonify({"ok": False, "error": "invalid agent credential"}), 401
        return view(agent, *args, **kwargs)

    return wrapped


def _timezone_name():
    row = db.session.get(AppSetting, "timezone")
    return row.value if row and row.value else os.getenv("APP_TIMEZONE", "UTC")


def _enqueue_due_jobs(agent):
    now_local = datetime.now(ZoneInfo(_timezone_name())).replace(tzinfo=None)
    now_utc = utcnow_naive()
    due_window = int(os.getenv("CRON_DUE_WINDOW_SECONDS", "60"))

    jobs = Job.query.filter_by(is_active=True, server=agent.agent_name).all()
    for job in jobs:
        try:
            scheduled_local = croniter(job.cron, now_local).get_prev(datetime)
        except (CroniterBadCronError, ValueError):
            continue
        if (now_local - scheduled_local).total_seconds() >= due_window:
            continue

        scheduled_utc = (
            scheduled_local.replace(tzinfo=ZoneInfo(_timezone_name()))
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
        )
        existing = (
            Run.query.filter(Run.job_id == job.id)
            .filter(Run.start_time >= scheduled_utc)
            .filter(Run.start_time < scheduled_utc + timedelta(seconds=due_window))
            .first()
        )
        if existing:
            continue

        db.session.add(
            Run(
                job_id=job.id,
                status="QUEUED",
                start_time=scheduled_utc,
                triggered_by="system",
            )
        )
    db.session.commit()


@agent_api.post("/register")
def register():
    payload = request.get_json(silent=True) or {}
    registration_token = (payload.get("registration_token") or "").strip()
    agent_name = (payload.get("agent_name") or "").strip()
    hostname = (payload.get("hostname") or agent_name).strip()

    if not registration_token or not agent_name:
        return jsonify({
            "ok": False,
            "error": "registration_token and agent_name are required",
        }), 400

    now = utcnow_naive()
    row = AgentRegistrationToken.query.filter_by(
        token_hash=token_hash(registration_token)
    ).first()
    if row is None or row.used_at is not None or row.expires_at < now:
        return jsonify({"ok": False, "error": "invalid or expired setup token"}), 401
    if AgentRegistry.query.filter_by(agent_name=agent_name).first():
        return jsonify({"ok": False, "error": "agent name already registered"}), 409

    credential = secrets.token_urlsafe(48)
    agent = AgentRegistry(
        agent_name=agent_name,
        hostname=hostname,
        ip_address=request.remote_addr,
        pid=payload.get("pid"),
        platform=(payload.get("platform") or "")[:120],
        version=(payload.get("version") or "")[:50],
        token_hash=token_hash(credential),
        started_at=now,
        last_seen=now,
    )
    row.used_at = now
    db.session.add(agent)
    db.session.commit()

    return jsonify({
        "ok": True,
        "agent_name": agent.agent_name,
        "agent_token": credential,
        "poll_interval": int(os.getenv("AGENT_POLL_INTERVAL", "10")),
    }), 201


@agent_api.post("/heartbeat")
@agent_required
def heartbeat(agent):
    payload = request.get_json(silent=True) or {}
    agent.last_seen = utcnow_naive()
    agent.hostname = (payload.get("hostname") or agent.hostname)[:255]
    agent.ip_address = request.remote_addr
    agent.pid = payload.get("pid")
    agent.platform = (payload.get("platform") or agent.platform or "")[:120]
    agent.version = (payload.get("version") or agent.version or "")[:50]
    db.session.commit()
    return jsonify({"ok": True})


@agent_api.post("/work/claim")
@agent_required
def claim_work(agent):
    agent.last_seen = utcnow_naive()
    db.session.commit()
    _enqueue_due_jobs(agent)

    queued = (
        Run.query.join(Job, Job.id == Run.job_id)
        .filter(Job.server == agent.agent_name)
        .filter(Run.status == "QUEUED")
        .order_by(Run.id.asc())
        .first()
    )
    if queued is None:
        return jsonify({"ok": True, "work": None})

    job = db.session.get(Job, queued.job_id)
    running = Run.query.filter_by(job_id=job.id, status="RUNNING").count()
    if running >= int(job.max_running or 1):
        return jsonify({"ok": True, "work": None})

    queued.status = "RUNNING"
    queued.start_time = utcnow_naive()
    db.session.commit()
    return jsonify({
        "ok": True,
        "work": {
            "run_id": queued.id,
            "job_id": job.id,
            "name": job.name,
            "command": job.command,
            "max_seconds": int(os.getenv("CRON_MAX_RUN_SECONDS", "86400")),
        },
    })


@agent_api.post("/runs/<int:run_id>/complete")
@agent_required
def complete_run(agent, run_id):
    run = db.get_or_404(Run, run_id)
    job = db.session.get(Job, run.job_id)
    if job is None or job.server != agent.agent_name:
        return jsonify({"ok": False, "error": "run does not belong to agent"}), 403

    payload = request.get_json(silent=True) or {}
    status = (payload.get("status") or "FAILED").upper()
    if status not in {"SUCCESS", "FAILED"}:
        status = "FAILED"
    log_text = str(payload.get("log") or "")
    max_log = int(os.getenv("AGENT_MAX_LOG_BYTES", "1048576"))

    run.status = status
    run.end_time = utcnow_naive()
    run.process_pid = None
    history = LogHistory.query.filter_by(run_id=run.id).first()
    if history is None:
        history = LogHistory(run_id=run.id, job_id=run.job_id, status=status)
        db.session.add(history)
    history.status = status
    history.start_time = run.start_time
    history.end_time = run.end_time
    history.log_text = log_text[-max_log:]
    db.session.commit()
    return jsonify({"ok": True})

