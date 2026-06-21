import json
import json
import hashlib
import os
import secrets
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

import psycopg2
from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import current_user, login_required
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

from .extensions import db
from .models import (
    AgentRegistrationToken,
    AgentRegistry,
    AppSetting,
    Job,
    UserETL,
)
from .backup_service import (
    create_backup,
    list_backups,
    resolve_stored_backup,
    restore_backup,
)


admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if (getattr(current_user, "role", "") or "").lower() != "admin":
            flash("Administrator access is required.", "error")
            return redirect(url_for("jobs"))
        return view(*args, **kwargs)

    return wrapped


def _job_groups():
    groups = defaultdict(list)
    for job in Job.query.order_by(Job.category.asc(), Job.name.asc()).all():
        groups[(job.category or "Uncategorized").strip() or "Uncategorized"].append(job)
    return dict(groups)


def _selected_job_ids(user):
    return set(user.allowed_job_ids()) if user else set()


def _serialize_job_ids(values):
    ids = []
    for value in values:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ",".join(str(value) for value in sorted(set(ids)))


def _setting(key, default=""):
    row = db.session.get(AppSetting, key)
    return row.value if row else default


def _set_setting(key, value):
    row = db.session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)


def _current_database_form():
    return {
        "host": os.getenv("DB_HOST", "postgres"),
        "port": os.getenv("DB_PORT", "5432"),
        "name": os.getenv("DB_NAME", "nrunmesh"),
        "user": os.getenv("DB_USER", "nrunmesh"),
        "password": "",
    }


def _database_values_from_request():
    current = _current_database_form()
    saved_raw = _setting("pending_database_connection", "")
    if saved_raw:
        try:
            current.update(json.loads(saved_raw))
        except (TypeError, ValueError):
            pass

    return {
        "host": (request.form.get("db_host") or current["host"]).strip(),
        "port": (request.form.get("db_port") or current["port"]).strip(),
        "name": (request.form.get("db_name") or current["name"]).strip(),
        "user": (request.form.get("db_user") or current["user"]).strip(),
        "password": request.form.get("db_password") or os.getenv("DB_PASSWORD", ""),
    }


def _test_database(values):
    connection = psycopg2.connect(
        host=values["host"],
        port=int(values["port"]),
        dbname=values["name"],
        user=values["user"],
        password=values["password"],
        connect_timeout=5,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version()")
            return cursor.fetchone()[0]
    finally:
        connection.close()


@admin_bp.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        display_name = (request.form.get("display_name") or "").strip()
        password = request.form.get("password") or ""
        role = (request.form.get("role") or "viewer").strip().lower()

        if role not in {"admin", "operator", "viewer"}:
            flash("Invalid role.", "error")
        elif not username or not password:
            flash("Username and password are required.", "error")
        elif db.session.get(UserETL, username):
            flash(f"User '{username}' already exists.", "error")
        else:
            user = UserETL(
                u_user=username,
                u_name=display_name or username,
                u_pass=generate_password_hash(password),
                role=role,
                jobs_viewer_list=(
                    "" if role == "admin"
                    else _serialize_job_ids(request.form.getlist("job_ids"))
                ),
            )
            db.session.add(user)
            db.session.commit()
            flash(f"User '{username}' created.", "success")
            return redirect(url_for("admin.users"))

    return render_template(
        "admin/users.html",
        users=UserETL.query.order_by(UserETL.u_user.asc()).all(),
        job_groups=_job_groups(),
    )


@admin_bp.route("/users/<username>/edit", methods=["GET", "POST"])
@admin_required
def edit_user(username):
    user = db.get_or_404(UserETL, username)

    if request.method == "POST":
        role = (request.form.get("role") or "viewer").strip().lower()
        if role not in {"admin", "operator", "viewer"}:
            flash("Invalid role.", "error")
        else:
            user.u_name = (request.form.get("display_name") or username).strip()
            user.role = role
            user.jobs_viewer_list = (
                "" if role == "admin"
                else _serialize_job_ids(request.form.getlist("job_ids"))
            )
            new_password = request.form.get("password") or ""
            if new_password:
                user.u_pass = generate_password_hash(new_password)
            db.session.commit()
            flash(f"User '{username}' updated.", "success")
            return redirect(url_for("admin.users"))

    return render_template(
        "admin/user_edit.html",
        user=user,
        job_groups=_job_groups(),
        selected_job_ids=_selected_job_ids(user),
    )


@admin_bp.post("/users/<username>/delete")
@admin_required
def delete_user(username):
    user = db.get_or_404(UserETL, username)
    if user.u_user == current_user.u_user:
        flash("You cannot delete your own active account.", "error")
    else:
        db.session.delete(user)
        db.session.commit()
        flash(f"User '{username}' deleted.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.get("/agents")
@login_required
def agents():
    ttl = int(os.getenv("CRON_AGENT_TTL", "60"))
    rows = AgentRegistry.query.order_by(AgentRegistry.agent_name.asc()).all()
    setup_token = session.pop("new_agent_setup_token", None)
    return render_template(
        "admin/agents.html",
        agents=rows,
        agent_ttl=ttl,
        setup_token=setup_token,
        controller_url=request.host_url.rstrip("/"),
    )


@admin_bp.post("/agents/setup-token")
@admin_required
def create_agent_setup_token():
    label = (request.form.get("label") or "Agent setup").strip()[:120]
    hours = int(request.form.get("expires_hours") or "1")
    hours = min(max(hours, 1), 168)
    raw_token = "nrm_setup_" + secrets.token_urlsafe(32)
    row = AgentRegistrationToken(
        token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        label=label,
        expires_at=datetime.utcnow() + timedelta(hours=hours),
        created_by=current_user.u_user,
    )
    db.session.add(row)
    db.session.commit()
    session["new_agent_setup_token"] = raw_token
    return redirect(url_for("admin.agents"))


@admin_bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "timezone":
            timezone_name = (request.form.get("timezone") or "UTC").strip()
            try:
                ZoneInfo(timezone_name)
                _set_setting("timezone", timezone_name)
                db.session.commit()
                flash(f"Timezone changed to {timezone_name}.", "success")
            except Exception:
                db.session.rollback()
                flash("Invalid timezone.", "error")

        elif action == "auto_backup":
            enabled = "1" if request.form.get("auto_backup_enabled") == "1" else "0"
            schedule_time = (request.form.get("auto_backup_time") or "09:00").strip()
            directory = (request.form.get("auto_backup_directory") or "daily").strip()
            retention = (request.form.get("auto_backup_retention_days") or "30").strip()

            try:
                datetime.strptime(schedule_time, "%H:%M")
                retention_days = int(retention)
                if retention_days < 0 or retention_days > 3650:
                    raise ValueError("Retention must be between 0 and 3650 days.")

                from .backup_service import BACKUP_ROOT, backup_subdirectory
                _set_setting("auto_backup_directory", directory)
                backup_subdirectory()
                _set_setting("auto_backup_enabled", enabled)
                _set_setting("auto_backup_time", schedule_time)
                _set_setting("auto_backup_retention_days", retention_days)
                db.session.commit()
                flash(
                    f"Automatic backup settings saved. Storage root: {BACKUP_ROOT}",
                    "success",
                )
            except Exception as exc:
                db.session.rollback()
                flash(f"Invalid backup settings: {exc}", "error")

        elif action in {"test_database", "save_database"}:
            values = _database_values_from_request()
            try:
                version = _test_database(values)
                if action == "save_database":
                    safe_values = dict(values)
                    safe_values["password"] = ""
                    _set_setting(
                        "pending_database_connection",
                        json.dumps(safe_values),
                    )
                    db.session.commit()
                    flash(
                        "Connection verified and saved as pending. Apply these "
                        "values to .env and restart the Controller.",
                        "success",
                    )
                else:
                    flash(f"Connection successful: {version}", "success")
            except Exception as exc:
                db.session.rollback()
                current_app.logger.exception("Database connection test failed")
                flash(f"Database connection failed: {exc}", "error")

        return redirect(url_for("admin.settings"))

    pending_database = _current_database_form()
    pending_raw = _setting("pending_database_connection", "")
    if pending_raw:
        try:
            pending_database.update(json.loads(pending_raw))
        except (TypeError, ValueError):
            pass
    pending_database["password"] = ""

    return render_template(
        "admin/settings.html",
        current_timezone=_setting("timezone", os.getenv("APP_TIMEZONE", "UTC")),
        timezones=sorted(available_timezones()),
        database=pending_database,
        has_pending_database=bool(pending_raw),
        auto_backup={
            "enabled": _setting("auto_backup_enabled", "1") == "1",
            "time": _setting("auto_backup_time", "09:00"),
            "directory": _setting("auto_backup_directory", "daily"),
            "retention_days": _setting("auto_backup_retention_days", "30"),
            "last_status": _setting("auto_backup_last_status", "never"),
            "last_file": _setting("auto_backup_last_file", ""),
            "last_error": _setting("auto_backup_last_error", ""),
            "last_run": _setting("auto_backup_last_run", ""),
        },
        backups=list_backups(),
        backup_root=os.getenv("BACKUP_ROOT", "/backups"),
        backup_host_path=os.getenv("BACKUP_HOST_PATH", "./backups"),
    )


@admin_bp.get("/settings/backup")
@admin_required
def backup_database():
    try:
        backup_path = create_backup()
    except Exception as exc:
        current_app.logger.exception("Manual backup failed")
        flash(f"Backup failed: {exc}", "error")
        return redirect(url_for("admin.settings"))

    flash(f"Backup created: {backup_path.name}", "success")
    return redirect(url_for("admin.settings"))


@admin_bp.get("/settings/backups/download")
@admin_required
def download_stored_backup():
    try:
        backup_path = resolve_stored_backup(request.args.get("path"))
    except Exception as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.settings"))
    return send_file(
        backup_path,
        as_attachment=True,
        download_name=backup_path.name,
        mimetype="application/octet-stream",
    )


@admin_bp.post("/settings/backups/restore")
@admin_required
def restore_stored_backup():
    confirmation = (request.form.get("confirmation") or "").strip()
    if confirmation != "RESTORE":
        flash("Type RESTORE to confirm this destructive operation.", "error")
        return redirect(url_for("admin.settings"))
    try:
        backup_path = resolve_stored_backup(request.form.get("path"))
        restore_backup(backup_path)
        db.session.remove()
        flash(f"Database restored from {backup_path.name}.", "success")
    except Exception as exc:
        current_app.logger.exception("Stored backup restore failed")
        flash(f"Restore failed: {exc}", "error")
    return redirect(url_for("admin.settings"))


@admin_bp.post("/settings/backups/delete")
@admin_required
def delete_stored_backups():
    selected_paths = request.form.getlist("backup_paths")
    if not selected_paths:
        flash("Select at least one backup to delete.", "error")
        return redirect(url_for("admin.settings"))

    resolved = []
    try:
        for relative_path in selected_paths:
            resolved.append(resolve_stored_backup(relative_path))
    except Exception as exc:
        flash(f"Backup deletion rejected: {exc}", "error")
        return redirect(url_for("admin.settings"))

    deleted = 0
    errors = []
    for backup_path in resolved:
        try:
            backup_path.unlink()
            deleted += 1
        except OSError as exc:
            errors.append(f"{backup_path.name}: {exc}")

    if deleted:
        flash(f"{deleted} backup file(s) deleted.", "success")
    if errors:
        flash("Some backups could not be deleted: " + "; ".join(errors), "error")
    return redirect(url_for("admin.settings"))


@admin_bp.post("/settings/restore")
@admin_required
def restore_database():
    upload = request.files.get("backup_file")
    confirmation = (request.form.get("confirmation") or "").strip()

    if confirmation != "RESTORE":
        flash("Type RESTORE to confirm this destructive operation.", "error")
        return redirect(url_for("admin.settings"))
    if not upload or not upload.filename:
        flash("Choose a PostgreSQL .dump backup file.", "error")
        return redirect(url_for("admin.settings"))

    filename = secure_filename(upload.filename)
    if not filename.lower().endswith(".dump"):
        flash("Only PostgreSQL custom-format .dump files are accepted.", "error")
        return redirect(url_for("admin.settings"))

    with tempfile.TemporaryDirectory(prefix="nrunmesh-restore-") as temp_dir:
        backup_path = Path(temp_dir) / filename
        upload.save(backup_path)

        try:
            restore_backup(backup_path)
        except Exception as exc:
            current_app.logger.exception("Uploaded backup restore failed")
            flash(f"Restore failed: {exc}", "error")
            return redirect(url_for("admin.settings"))

    db.session.remove()
    flash("Database restored. Refresh the application and verify your data.", "success")
    return redirect(url_for("admin.settings"))
