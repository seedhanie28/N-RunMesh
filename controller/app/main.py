import os
import hashlib
from datetime import datetime, date
import requests
from werkzeug.security import generate_password_hash
from typing import List, Tuple, Optional
from flask import Flask, render_template, request, redirect, url_for, flash, make_response, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import text, func, case
from sqlalchemy.orm import aliased
from app.utils.timezone import format_local, to_local, utcnow
from .scheduler import tick, run_now as run_job_local

from .config import Config
from .extensions import db, login_manager
from .models import Job, Run, LogHistory, UserETL, AgentRegistry, AppSetting

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)
login_manager.init_app(app)
login_manager.login_view = "login"

from .admin import admin_bp
app.register_blueprint(admin_bp)
from .agent_api import agent_api
app.register_blueprint(agent_api)


@app.context_processor
def inject_helpers():
    def fmt_runtime(start_dt, end_dt):
        """Return HH:MM:SS between start_dt and end_dt (naive/aware safe)."""
        if not start_dt or not end_dt:
            return "-"
        try:
            # If tz-aware, make both naive for subtraction
            if getattr(start_dt, "tzinfo", None) is not None and getattr(end_dt, "tzinfo", None) is not None:
                start = start_dt.replace(tzinfo=None)
                end = end_dt.replace(tzinfo=None)
            else:
                start = start_dt
                end = end_dt
            sec = int(max(0, (end - start).total_seconds()))
        except Exception:
            return "-"

        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    agent_ttl = int(os.environ.get("CRON_AGENT_TTL", "60"))
    agents = AgentRegistry.query.all() if current_user.is_authenticated else []
    agent_online_count = sum(1 for agent in agents if agent.is_online(agent_ttl))

    return dict(
        format_local=format_local,
        fmt_runtime=fmt_runtime,
        agent_online_count=agent_online_count,
        agent_total_count=len(agents),
    )


@app.get("/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"ok": True, "service": "nrunmesh-controller"})
    except Exception:
        db.session.rollback()
        return jsonify({
            "ok": False,
            "service": "nrunmesh-controller",
            "error": "database unavailable",
        }), 503

@login_manager.user_loader
def load_user(user_id):
    # UserETL PK = u_user (string)
    try:
        return UserETL.query.get(str(user_id))
    except Exception:
        app.logger.exception("load_user failed for user_id=%r", user_id)
        return None

def _is_md5_hex(s: str) -> bool:
    s = (s or "").strip()
    return len(s) == 32 and all(c in "0123456789abcdefABCDEF" for c in s)


def _get_stored_password(u) -> Tuple[str, Optional[str]]:
    """Return (stored_password, field_name) for common password fields."""
    for attr in ("password_hash", "u_pass", "u_password", "u_pwd", "password"):
        if hasattr(u, attr):
            val = getattr(u, attr)
            if val is None:
                continue
            val_str = str(val).strip()
            if val_str != "":
                return val_str, attr
    return "", None


def _verify_password(u, pw: str) -> bool:
    """Verify password against what's stored in DB.

    Supports:
    - MD5 hex stored
    - Werkzeug hash stored (pbkdf2/scrypt/argon2/bcrypt/etc)
    - fallback plain-text compare
    - if the model implements `check_password`, we try that too
    """
    pw = pw or ""

    stored, _field = _get_stored_password(u)
    if not stored:
        return False

    # 1) MD5 (common in legacy systems)
    if _is_md5_hex(stored):
        calc = hashlib.md5(pw.encode("utf-8")).hexdigest()
        return calc.lower() == stored.lower()

    # 2) If model has a check_password method, try it
    if hasattr(u, "check_password"):
        try:
            ok = bool(u.check_password(pw))
            if ok:
                return True
        except Exception:
            # Don't fail hard; we'll try other strategies below
            app.logger.exception("UserETL.check_password() threw for user=%r", getattr(u, "u_user", None))

    # 3) Werkzeug hashes
    # Many werkzeug hashes look like: "pbkdf2:sha256:..." / "scrypt:..." / "argon2:..." / "bcrypt:..."
    try:
        from werkzeug.security import check_password_hash

        # check_password_hash will raise if format isn't recognized
        if any(stored.startswith(p) for p in ("pbkdf2:", "scrypt:", "argon2:", "bcrypt:", "sha256:")):
            return bool(check_password_hash(stored, pw))

        # If it's not clearly a werkzeug string, still try once (safe; catch exceptions)
        return bool(check_password_hash(stored, pw))
    except Exception:
        # 4) Legacy fallback: plain-text compare
        return stored == pw


def _job_query_for_current_user():
    q = Job.query
    if not current_user.is_authenticated:
        return q.filter(text("1=0"))

    role = (getattr(current_user, "role", "") or "").lower()
    if role == "admin":
        return q

    allowed = []
    try:
        allowed = current_user.allowed_job_ids()  # type: ignore[attr-defined]
    except Exception:
        allowed = []

    if not allowed:
        return q.filter(text("1=0"))

    return q.filter(Job.id.in_(allowed))


def _persist_run_log_to_db(run: Run):
    """Persist the last ~2000 log lines of a run into LogHistory (idempotent)."""
    existing = LogHistory.query.filter_by(run_id=run.id).first()
    if existing:
        return existing

    content = ""
    if run.log_path and os.path.exists(run.log_path):
        with open(run.log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            content = "".join(lines[-2000:])

    h = LogHistory(
        run_id=run.id,
        job_id=run.job_id,
        status=run.status or "",
        start_time=run.start_time,
        end_time=run.end_time,
        log_text=content,
    )
    db.session.add(h)
    db.session.commit()
    return h

def _agent_base_url(agent: AgentRegistry) -> str:
    # Prefer stored IP; fallback to hostname; last resort agent_name
    host = (agent.ip_address or "").strip() or (agent.hostname or "").strip() or (agent.agent_name or "").strip()
    port = int(os.environ.get("CRON_AGENT_HTTP_PORT", "8765"))
    return f"http://{host}:{port}"

def _run_job_locally(job: Job):
    try:
        run = run_job_local(job)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Local run failed: {e}"}), 400

    try:
        run.triggered_by = (getattr(current_user, "u_user", None) or current_user.get_id() or "manual")
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify({
        "ok": True,
        "run_id": run.id,
        "status": run.status,
        "mode": "local",
    })

def _agent_headers() -> dict:
    api_key = (os.environ.get("CRON_AGENT_API_KEY") or "").strip()
    if not api_key:
        return {}

    configured_header = (os.environ.get("CRON_AGENT_AUTH_HEADER") or "").strip()
    if configured_header:
        return {configured_header: api_key}

    return {
        "X-API-Key": api_key,
        "X-Agent-Key": api_key,
        "X-Auth-Token": api_key,
        "Authorization": f"Bearer {api_key}",
    }

@app.route("/")
def index():
    # kalau sudah login (session masih ada) langsung ke jobs
    if current_user.is_authenticated:
        return redirect(url_for("jobs"))
    # kalau belum login, ke login
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    username_for_log = None

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        username_for_log = username
        password = request.form.get("password") or ""

        if not username or not password:
            error = "Username / password masih kosong."
        else:
            try:
                u = UserETL.query.filter_by(u_user=username).first()

                if not u:
                    error = f"User '{username}' tidak ditemukan."
                else:
                    try:
                        ok = _verify_password(u, password)
                    except Exception as e:
                        app.logger.exception("Password verification error for user=%s", username)
                        error = f"Error saat verifikasi password: {e!r}"
                    else:
                        if ok:
                            app.logger.info("Login success user=%s", username)
                            login_user(u)
                            # support ?next=... from Flask-Login
                            next_url = request.args.get("next")
                            return redirect(next_url or url_for("jobs"))
                        error = "Password salah."

            except Exception as e:
                app.logger.exception("Login error (query/db) for user=%s", username)
                error = f"Login error (query/db): {e!r}"

    # Show on UI (if template supports it) + flash as fallback.
    if error:
        try:
            flash(error, "error")
        except Exception:
            app.logger.exception("flash() failed (SECRET_KEY missing?)")

    html = render_template("login.html", error=error)

    status = 200
    if error:
        status = 401
        app.logger.warning("Login failed user=%s reason=%s", username_for_log, error)
        html = f"{html}\n<!-- LOGIN_ERROR: {error} -->\n"

    resp = make_response(html, status)
    if error:
        resp.headers["X-Login-Error"] = error
    return resp


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "profile":
            current_user.u_name = (
                request.form.get("display_name") or current_user.u_user
            ).strip()
            db.session.commit()
            flash("Profile updated.", "success")
        elif action == "password":
            current_password = request.form.get("current_password") or ""
            new_password = request.form.get("new_password") or ""
            confirm_password = request.form.get("confirm_password") or ""
            if not current_user.check_password(current_password):
                flash("Current password is incorrect.", "error")
            elif len(new_password) < 8:
                flash("New password must contain at least 8 characters.", "error")
            elif new_password != confirm_password:
                flash("New password confirmation does not match.", "error")
            else:
                current_user.u_pass = generate_password_hash(new_password)
                db.session.commit()
                flash("Password changed.", "success")
        return redirect(url_for("profile"))

    return render_template("profile.html")

@app.route("/jobs", methods=["GET", "POST"])
@login_required
def jobs():
    # create job (admin only)
    if request.method == "POST" and (getattr(current_user, "role", "") or "").lower() == "admin":
        server_val = request.form.get("server")
        if server_val == "__manual__":
            server_val = (request.form.get("server_manual") or "").strip()
        if not server_val:
            server_val = "default"

        j = Job(
            name=request.form["name"],
            category=request.form.get("category") or "",
            server=server_val,
            command=request.form["command"],
            cron=request.form["cron"],
            max_running=int(request.form.get("max_running") or 1),
            is_active=True,
        )
        db.session.add(j)
        db.session.commit()
        return redirect(url_for("jobs"))

    # list + filters + paging
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 10))
    category = (request.args.get("category") or "").strip()
    name = (request.args.get("name") or "").strip()

    query = _job_query_for_current_user()
    if category:
        query = query.filter(Job.category == category)
    if name:
        query = query.filter(Job.name.like(f"%{name}%"))

    pagination = query.order_by(Job.id.desc()).paginate(
        page=page,
        per_page=per_page,
        error_out=False,
    )

    # last run per job (by Run.id) so we can also show status
    last_run_subq = (
        db.session.query(
            Run.job_id,
            func.max(Run.id).label("last_run_id"),
        )
        .group_by(Run.job_id)
        .subquery()
    )

    RunLast = aliased(Run)

    jobs_with_last = (
        db.session.query(Job, RunLast.start_time, RunLast.status)
        .outerjoin(last_run_subq, Job.id == last_run_subq.c.job_id)
        .outerjoin(RunLast, RunLast.id == last_run_subq.c.last_run_id)
        .filter(Job.id.in_([j.id for j in pagination.items]))
        .all()
    )

    last_exec_map = {job.id: st for job, st, _status in jobs_with_last}
    last_status_map = {job.id: (_status or "") for job, _st, _status in jobs_with_last}


    # server dropdown: show all registered agents (UI only)
    servers = []  # type: List[str]
    try:
        servers = [
            a.agent_name
            for a in AgentRegistry.query.order_by(AgentRegistry.agent_name.asc()).all()
        ]
    except Exception:
        servers = []

    # category suggestions (for dropdown-like typing)
    categories = []
    try:
        rows = (
            db.session.query(Job.category)
            .filter(Job.category.isnot(None))
            .filter(Job.category != "")
            .distinct()
            .order_by(Job.category.asc())
            .all()
        )
        categories = [r[0] for r in rows if r and r[0]]
    except Exception:
        categories = []

    return render_template(
        "jobs.html",
        jobs=pagination.items,
        pagination=pagination,
        category=category,
        name=name,
        servers=servers,
        last_exec_map=last_exec_map,
        categories=categories,
        last_status_map=last_status_map
    )


@app.route("/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@login_required
def edit_job(job_id):
    if (getattr(current_user, "role", "") or "").lower() != "admin":
        return "Forbidden", 403

    job = Job.query.get_or_404(job_id)

    if request.method == "POST":
        job.name = request.form["name"]
        job.category = request.form.get("category") or ""
        server_val = request.form.get("server")
        if server_val == "__manual__":
            server_val = (request.form.get("server_manual") or "").strip()
        if not server_val:
            server_val = job.server or "default"
        job.server = server_val
        job.command = request.form["command"]
        job.cron = request.form["cron"]
        job.max_running = int(request.form.get("max_running") or 1)
        job.is_active = True if request.form.get("is_active") == "on" else False

        db.session.commit()
        return redirect(url_for("jobs"))

    # server dropdown: show all registered agents (UI only)
    servers = []  # type: List[str]
    try:
        servers = [
            a.agent_name
            for a in AgentRegistry.query.order_by(AgentRegistry.agent_name.asc()).all()
        ]
    except Exception:
        servers = []

    categories = []
    try:
        rows = (
            db.session.query(Job.category)
            .filter(Job.category.isnot(None))
            .filter(Job.category != "")
            .distinct()
            .order_by(Job.category.asc())
            .all()
        )
        categories = [r[0] for r in rows if r and r[0]]
    except Exception:
        categories = []

    return render_template("job_edit.html", job=job, servers=servers, categories=categories)


@app.route("/jobs/<int:job_id>/delete", methods=["POST"])
@login_required
def delete_job(job_id):
    if (getattr(current_user, "role", "") or "").lower() != "admin":
        return "Forbidden", 403
    job = Job.query.get_or_404(job_id)
    db.session.delete(job)
    db.session.commit()
    return redirect(url_for("jobs"))


@app.route("/runs")
@login_required
def runs():
    job_id = request.args.get("job_id")
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))

    # default: show only live/active runs
    show_all = (request.args.get("all") or "").lower() in ("1", "true", "yes")

    query = (
        db.session.query(Run, Job.name, Job.category)
        .join(Job, Job.id == Run.job_id)
    )

    if not show_all:
        query = query.filter(Run.status.in_(["QUEUED", "RUNNING"]))

    role = (getattr(current_user, "role", "") or "").lower()
    if role != "admin":
        allowed = current_user.allowed_job_ids() if hasattr(current_user, "allowed_job_ids") else []
        if not allowed:
            query = query.filter(text("1=0"))
        else:
            query = query.filter(Run.job_id.in_(allowed))

    if job_id:
        try:
            query = query.filter(Run.job_id == int(job_id))
        except ValueError:
            pass

    pagination = query.order_by(Run.id.desc()).paginate(
        page=page,
        per_page=per_page,
        error_out=False,
    )

    return render_template(
        "runs.html",
        runs=pagination.items,
        pagination=pagination,
        job_id=job_id,
        to_local=to_local,
        show_all=show_all,
    )


@app.route("/history")
@login_required
def history():
    job_id = request.args.get("job_id")
    status = request.args.get("status")

    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))

    # history reads from LogHistory (persisted), joined with Job for name/category
    query = (
        db.session.query(LogHistory, Job.name, Job.category, Job.server)
        .join(Job, Job.id == LogHistory.job_id)
    )

    role = (getattr(current_user, "role", "") or "").lower()
    if role != "admin":
        allowed = current_user.allowed_job_ids() if hasattr(current_user, "allowed_job_ids") else []
        if not allowed:
            query = query.filter(text("1=0"))
        else:
            query = query.filter(LogHistory.job_id.in_(allowed))

    if job_id:
        try:
            query = query.filter(LogHistory.job_id == int(job_id))
        except ValueError:
            pass

    if status:
        query = query.filter(LogHistory.status == status)

    pagination = query.order_by(LogHistory.id.desc()).paginate(
        page=page,
        per_page=per_page,
        error_out=False,
    )

    # ---- ETL STATS (simple) ----
    try:
        today = date.today()

        base_q = (
            db.session.query(
                func.count(LogHistory.run_id).label("total_runs"),
                func.sum(case((LogHistory.status == "SUCCESS", 1), else_=0)).label("success_cnt"),
                func.sum(case((LogHistory.status == "FAILED", 1), else_=0)).label("failed_cnt"),
                func.avg(func.extract("epoch", LogHistory.end_time - LogHistory.start_time)).label("avg_sec"),
            )
        )

        today_row = base_q.filter(func.date(LogHistory.start_time) == today).one()
        mtd_row = base_q.filter(
            func.date_trunc("month", LogHistory.start_time) == func.date_trunc("month", func.now())
        ).one()
        ytd_row = base_q.filter(
            func.date_trunc("year", LogHistory.start_time) == func.date_trunc("year", func.now())
        ).one()

        etl_stats = {
            "total_jobs": db.session.query(func.count(Job.id)).scalar() or 0,
            "total_category": db.session.query(func.count(func.distinct(Job.category))).scalar() or 0,
            "total_server": db.session.query(func.count(func.distinct(Job.server))).scalar() or 0,
            "today": today_row,
            "mtd": mtd_row,
            "ytd": ytd_row,
        }
    except Exception:
        app.logger.exception("Failed building etl_stats")

        class _Row:
            total_runs = 0
            success_cnt = 0
            failed_cnt = 0
            avg_sec = 0

        etl_stats = {
            "total_jobs": 0,
            "total_category": 0,
            "total_server": 0,
            "today": _Row(),
            "mtd": _Row(),
            "ytd": _Row(),
        }

    return render_template(
        "history.html",
        runs=pagination.items,  # tuples: (LogHistory, job_name, category)
        pagination=pagination,
        job_id=job_id,
        status=status,
        to_local=to_local,
        etl_stats=etl_stats,
    )


@app.route("/runs/<int:run_id>/log")
@login_required
def view_log(run_id):
    run = Run.query.get_or_404(run_id)

    role = (getattr(current_user, "role", "") or "").lower()
    if role != "admin":
        allowed = current_user.allowed_job_ids() if hasattr(current_user, "allowed_job_ids") else []
        if run.job_id not in allowed:
            return "Forbidden", 403

    hist = LogHistory.query.filter_by(run_id=run.id).first()
    if not hist:
        hist = _persist_run_log_to_db(run)

    content = hist.log_text if hist and hist.log_text else ""
    return render_template("log.html", run=run, content=content)

@app.route("/jobs/<int:job_id>/runs")
@login_required
def job_runs(job_id):
    job = Job.query.get_or_404(job_id)

    if current_user.role != "admin":
        allowed = current_user.allowed_job_ids()
        if job.id not in allowed:
            return "Forbidden", 403

    runs = (
        Run.query
        .filter(Run.job_id == job_id)
        .order_by(Run.start_time.desc())
        .limit(50)
        .all()
    )

    return render_template(
        "job_runs.html",
        job=job,
        runs=runs,
        to_local=to_local,
    )

@app.route("/jobs/<int:job_id>/run", methods=["POST"])
@login_required
def run_job_now(job_id):
    role = (getattr(current_user, "role", "") or "").lower()
    if role not in {"admin", "operator"}:
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    job = _job_query_for_current_user().filter(Job.id == job_id).first_or_404()

    agent = AgentRegistry.query.filter_by(agent_name=job.server).first()
    if not agent:
        return _run_job_locally(job)

    # best-effort: block if agent offline
    try:
        ttl = int(os.environ.get("CRON_AGENT_TTL", "60"))
        now = utcnow().replace(tzinfo=None)  # keep comparison naive UTC
        last = agent.last_seen
        if (not last) or ((now - last).total_seconds() > ttl):
            return jsonify({"ok": False, "error": f"Agent '{job.server}' offline"}), 400
    except Exception:
        pass

    run = Run(
        job_id=job.id,
        status="QUEUED",
        triggered_by=(
            getattr(current_user, "u_user", None)
            or current_user.get_id()
            or "manual"
        ),
    )
    db.session.add(run)
    db.session.commit()
    return jsonify({
        "ok": True,
        "agent": job.server,
        "run_id": run.id,
        "status": "QUEUED",
        "mode": "agent-poll",
    })

@app.route("/runs/<int:run_id>/log_text")
@login_required
def run_log_text(run_id):
    run = Run.query.get_or_404(run_id)
    job = Job.query.get(run.job_id)

    # permission
    if current_user.role != "admin":
        allowed = current_user.allowed_job_ids() if hasattr(current_user, "allowed_job_ids") else []
        if (job is None) or (job.id not in allowed):
            return "Forbidden", 403

    status = (run.status or "").upper()

    # 1) RUNNING → realtime log dari agent
    if status == "RUNNING" and job is not None:
        try:
            agent = AgentRegistry.query.filter_by(agent_name=job.server).first()
            if agent and not agent.token_hash:
                base = _agent_base_url(agent)
                url = f"{base}/runs/{run.id}/log"

                resp = requests.get(url, headers=_agent_headers(), timeout=6)
                if resp.status_code < 400:
                    data = resp.json()
                    return jsonify({
                        "status": data.get("status") or run.status,
                        "content": data.get("content") or ""
                    })
        except Exception:
            pass  # fallback ke DB

    # 2) FINISHED → ambil dari DB
    content = ""
    hist = LogHistory.query.filter_by(run_id=run.id).first()
    if hist and hist.log_text:
        content = hist.log_text

    return jsonify({
        "status": run.status,
        "content": content
    })

@app.route("/runs/<int:run_id>/signal", methods=["POST"])
@login_required
def signal_run(run_id):
    if (getattr(current_user, "role", "") or "").lower() != "admin":
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    run = Run.query.get_or_404(run_id)
    job = Job.query.get(run.job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404

    agent = AgentRegistry.query.filter_by(agent_name=job.server).first()
    if not agent:
        return jsonify({"ok": False, "error": f"Agent '{job.server}' not found"}), 400
    if agent.token_hash:
        return jsonify({
            "ok": False,
            "error": "Stop signal for API-only agents is not available yet.",
        }), 501

    base = _agent_base_url(agent)
    url = f"{base}/runs/{run.id}/signal"

    payload = request.get_json(silent=True) or {}
    sig = (payload.get("signal") or "TERM").upper()

    try:
        resp = requests.post(url, headers=_agent_headers(), json={"signal": sig}, timeout=8)
        if resp.status_code >= 400:
            return jsonify({"ok": False, "error": f"Agent error {resp.status_code}: {resp.text}"}), 400
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"ok": False, "error": f"HTTP agent call failed: {e}"}), 400
