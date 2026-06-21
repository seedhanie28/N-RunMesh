import argparse
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .backup_service import (
    cleanup_old_backups,
    create_backup,
    get_setting,
    set_setting,
)
from .bootstrap import wait_for_database
from .extensions import db
from .main import app


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("nrunmesh.backup-worker")


def run_backup(reason="automatic"):
    path = create_backup()
    deleted = cleanup_old_backups()
    set_setting("auto_backup_last_status", "success")
    set_setting("auto_backup_last_file", path.name)
    set_setting("auto_backup_last_error", "")
    set_setting("auto_backup_last_run", datetime.now(timezone.utc).isoformat())
    db.session.commit()
    logger.info(
        "%s backup created: %s (expired removed: %s)",
        reason,
        path,
        deleted,
    )
    return path


def scheduled_tick():
    enabled = get_setting("auto_backup_enabled", "1") == "1"
    if not enabled:
        return False

    timezone_name = get_setting("timezone", "UTC")
    schedule_time = get_setting("auto_backup_time", "09:00")
    now = datetime.now(ZoneInfo(timezone_name))
    today = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M")
    last_date = get_setting("auto_backup_last_schedule_date", "")

    if current_time == schedule_time and last_date != today:
        try:
            run_backup()
            set_setting("auto_backup_last_schedule_date", today)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            set_setting("auto_backup_last_status", "failed")
            set_setting("auto_backup_last_error", str(exc))
            set_setting("auto_backup_last_run", datetime.now(timezone.utc).isoformat())
            db.session.commit()
            logger.exception("Automatic backup failed")
        return True
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    wait_for_database()
    with app.app_context():
        if args.once:
            run_backup("manual worker test")
            return

        logger.info("Backup worker started.")
        while True:
            try:
                scheduled_tick()
            except Exception:
                db.session.rollback()
                logger.exception("Backup scheduler tick failed")
            time.sleep(20)


if __name__ == "__main__":
    main()
