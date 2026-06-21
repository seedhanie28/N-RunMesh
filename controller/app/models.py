from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from typing import List
from .extensions import db

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    category = db.Column(db.String(50))
    server = db.Column(db.String(120), default="default", index=True)
    command = db.Column(db.Text)
    cron = db.Column(db.String(50))
    max_running = db.Column(db.Integer, default=1)
    is_active = db.Column(db.Boolean, default=True)


class Run(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    # link to Job so templates can use r.job.server
    job_id = db.Column(db.Integer, db.ForeignKey('job.id'), index=True)
    job = db.relationship('Job', lazy='joined')

    start_time = db.Column(db.DateTime, default=datetime.utcnow)
    end_time = db.Column(db.DateTime)
    status = db.Column(db.String(20))
    log_path = db.Column(db.Text)

    # process pid of the running command on the agent
    process_pid = db.Column(db.Integer)
    triggered_by = db.Column(db.String(100))  # username / system

class LogHistory(db.Model):
    __tablename__ = "log_history"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, unique=True, nullable=False, index=True)
    job_id = db.Column(db.Integer, nullable=False, index=True)

    status = db.Column(db.String(20), nullable=False)
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime)

    log_text = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class UserETL(UserMixin, db.Model):
    __tablename__ = "user_etl"

    u_user = db.Column(db.String(100), primary_key=True)
    u_name = db.Column(db.String(200))
    u_pass = db.Column(db.String(500))
    role = db.Column(db.String(20))          # admin / viewer
    jobs_viewer_list = db.Column(db.Text)    # "1,2,3" atau "(1,2,3)"

    def get_id(self):
        return str(self.u_user)

    def check_password(self, raw_password: str) -> bool:
        if self.u_pass is None:
            return False
        stored = str(self.u_pass)

        # kalau hash (werkzeug), biasanya ada ":" dan panjang
        if ":" in stored and len(stored) > 20:
            try:
                return check_password_hash(stored, raw_password)
            except Exception:
                return stored == raw_password

        # fallback: plain text compare
        return stored == raw_password

    def allowed_job_ids(self):
        if not self.jobs_viewer_list:
            return []
        s = str(self.jobs_viewer_list).strip()
        for ch in ["(", ")", "[", "]", "{", "}"]:
            s = s.replace(ch, "")
        parts = [p.strip() for p in s.split(",") if p.strip()]
        out = []  # type: List[int]
        for p in parts:
            try:
                out.append(int(p))
            except ValueError:
                continue
        return out

class AgentRegistry(db.Model):
    __tablename__ = "agent_registry"

    id = db.Column(db.Integer, primary_key=True)
    agent_name = db.Column(db.String(120), unique=True, nullable=False, index=True)
    hostname = db.Column(db.String(255), nullable=False)
    ip_address = db.Column(db.String(64), nullable=True)
    pid = db.Column(db.Integer, nullable=True)
    last_seen = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    token_hash = db.Column(db.String(64), nullable=True, unique=True, index=True)
    platform = db.Column(db.String(120), nullable=True)
    version = db.Column(db.String(50), nullable=True)

    def is_online(self, ttl_seconds: int = 60) -> bool:
        if not self.last_seen:
            return False
        return (datetime.utcnow() - self.last_seen).total_seconds() <= ttl_seconds


class AgentRegistrationToken(db.Model):
    __tablename__ = "agent_registration_token"

    id = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    label = db.Column(db.String(120), nullable=False, default="Agent setup")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    created_by = db.Column(db.String(100), nullable=True)


class AppSetting(db.Model):
    __tablename__ = "app_setting"

    key = db.Column(db.String(120), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
