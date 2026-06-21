import logging
import os
import time

from sqlalchemy import text
from werkzeug.security import generate_password_hash

from .extensions import db
from .main import app
from .models import AppSetting, UserETL


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("nrunmesh.bootstrap")


def wait_for_database() -> None:
    retries = int(os.getenv("DB_BOOTSTRAP_RETRIES", "60"))
    interval = float(os.getenv("DB_BOOTSTRAP_INTERVAL", "2"))

    with app.app_context():
        for attempt in range(1, retries + 1):
            try:
                db.session.execute(text("SELECT 1"))
                logger.info("Database is ready.")
                return
            except Exception as exc:
                db.session.rollback()
                if attempt >= retries:
                    raise
                logger.warning(
                    "Database unavailable (%s/%s): %s",
                    attempt,
                    retries,
                    exc,
                )
                time.sleep(interval)


def bootstrap() -> None:
    wait_for_database()

    username = os.getenv("NRUNMESH_ADMIN_USER", "admin").strip()
    password = os.getenv("NRUNMESH_ADMIN_PASSWORD", "admin-change-me")
    display_name = os.getenv(
        "NRUNMESH_ADMIN_NAME",
        "N-RunMesh Administrator",
    ).strip()

    if not username or not password:
        raise RuntimeError(
            "NRUNMESH_ADMIN_USER and NRUNMESH_ADMIN_PASSWORD must not be empty"
        )

    with app.app_context():
        db.create_all()
        db.session.execute(text(
            "ALTER TABLE agent_registry "
            "ADD COLUMN IF NOT EXISTS token_hash VARCHAR(64)"
        ))
        db.session.execute(text(
            "ALTER TABLE agent_registry "
            "ADD COLUMN IF NOT EXISTS platform VARCHAR(120)"
        ))
        db.session.execute(text(
            "ALTER TABLE agent_registry "
            "ADD COLUMN IF NOT EXISTS version VARCHAR(50)"
        ))
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ix_agent_registry_token_hash ON agent_registry(token_hash)"
        ))
        db.session.commit()

        admin = db.session.get(UserETL, username)
        if admin is None:
            admin = UserETL(
                u_user=username,
                u_name=display_name or username,
                u_pass=generate_password_hash(password),
                role="admin",
                jobs_viewer_list="",
            )
            db.session.add(admin)
            db.session.commit()
            logger.info("Initial administrator '%s' created.", username)
        else:
            logger.info("Administrator '%s' already exists; password unchanged.", username)

        defaults = {
            "auto_backup_enabled": "1",
            "auto_backup_time": "09:00",
            "auto_backup_directory": "daily",
            "auto_backup_retention_days": "30",
        }
        changed = False
        for key, value in defaults.items():
            if db.session.get(AppSetting, key) is None:
                db.session.add(AppSetting(key=key, value=value))
                changed = True
        if changed:
            db.session.commit()
            logger.info("Default automatic backup settings created.")


if __name__ == "__main__":
    bootstrap()
