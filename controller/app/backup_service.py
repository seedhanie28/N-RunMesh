import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from .extensions import db
from .models import AppSetting


BACKUP_ROOT = Path(os.getenv("BACKUP_ROOT", "/backups")).resolve()


def get_setting(key, default=""):
    row = db.session.get(AppSetting, key)
    return row.value if row else default


def set_setting(key, value):
    row = db.session.get(AppSetting, key)
    if row is None:
        db.session.add(AppSetting(key=key, value=str(value)))
    else:
        row.value = str(value)


def backup_subdirectory():
    raw = get_setting("auto_backup_directory", "daily").strip().replace("\\", "/")
    path = PurePosixPath(raw or "daily")
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Backup folder must be a relative folder under backup storage.")
    return Path(*path.parts)


def backup_directory():
    target = (BACKUP_ROOT / backup_subdirectory()).resolve()
    if not target.is_relative_to(BACKUP_ROOT):
        raise ValueError("Backup folder escapes the mounted backup storage.")
    target.mkdir(parents=True, exist_ok=True)
    return target


def pg_env():
    env = os.environ.copy()
    env["PGPASSWORD"] = os.getenv("DB_PASSWORD", "")
    return env


def pg_args():
    return [
        "--host",
        os.getenv("DB_HOST", "postgres"),
        "--port",
        os.getenv("DB_PORT", "5432"),
        "--username",
        os.getenv("DB_USER", "nrunmesh"),
        "--dbname",
        os.getenv("DB_NAME", "nrunmesh"),
    ]


def create_backup(prefix="nrunmesh"):
    timezone_name = get_setting("timezone", os.getenv("APP_TIMEZONE", "UTC"))
    now = datetime.now(ZoneInfo(timezone_name))
    filename = f"{prefix}_{now.strftime('%Y%m%d_%H%M%S')}.dump"
    destination = backup_directory() / filename

    result = subprocess.run(
        ["pg_dump", *pg_args(), "--format=custom", "--file", str(destination)],
        env=pg_env(),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        raise RuntimeError(result.stderr.strip() or "pg_dump failed")
    return destination


def cleanup_old_backups():
    retention_days = max(
        0,
        int(get_setting("auto_backup_retention_days", "30")),
    )
    if retention_days == 0:
        return 0

    cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
    deleted = 0
    for path in BACKUP_ROOT.rglob("*.dump"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            deleted += 1
    return deleted


def list_backups():
    if not BACKUP_ROOT.exists():
        return []

    rows = []
    for path in BACKUP_ROOT.rglob("*.dump"):
        if not path.is_file():
            continue
        stat = path.stat()
        rows.append({
            "name": path.name,
            "relative_path": path.relative_to(BACKUP_ROOT).as_posix(),
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(
                stat.st_mtime,
                tz=timezone.utc,
            ),
        })
    return sorted(rows, key=lambda row: row["modified_at"], reverse=True)


def resolve_stored_backup(relative_path):
    raw = (relative_path or "").replace("\\", "/")
    candidate = (BACKUP_ROOT / Path(*PurePosixPath(raw).parts)).resolve()
    if not candidate.is_relative_to(BACKUP_ROOT):
        raise ValueError("Invalid backup path.")
    if not candidate.is_file() or candidate.suffix.lower() != ".dump":
        raise FileNotFoundError("Stored backup was not found.")
    return candidate


def restore_backup(path):
    verify = subprocess.run(
        ["pg_restore", "--list", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if verify.returncode != 0:
        raise ValueError("The file is not a valid PostgreSQL custom backup.")

    result = subprocess.run(
        [
            "pg_restore",
            *pg_args(),
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            str(path),
        ],
        env=pg_env(),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "pg_restore failed")

