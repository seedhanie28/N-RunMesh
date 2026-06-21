import os

TIMEZONE = os.environ.get("APP_TIMEZONE", "UTC")

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        "postgresql://scheduler:scheduler@localhost:5432/scheduler"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
