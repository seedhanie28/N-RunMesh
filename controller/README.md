# Simple Job Scheduler (Starter)

Cron-like job scheduler with Web UI.

Features:
- Python 3.12
- Flask + SQLAlchemy
- PostgreSQL
- Login (admin/operator/viewer)
- Job categories
- Max running
- Cron schedule
- Local execution
- View logs
- Gunicorn ready

## Run (dev)
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export FLASK_APP=app.main
flask run

## Run (prod)
gunicorn -w 2 -b 0.0.0.0:8000 app.main:app
