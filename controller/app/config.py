import os
from urllib.parse import quote_plus

TIMEZONE = os.environ.get("APP_TIMEZONE", "UTC")


def _database_url() -> str:
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit

    user = quote_plus(os.getenv("DB_USER", "nrunmesh"))
    password = quote_plus(os.getenv("DB_PASSWORD", "nrunmesh-change-me"))
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = quote_plus(os.getenv("DB_NAME", "nrunmesh"))
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
