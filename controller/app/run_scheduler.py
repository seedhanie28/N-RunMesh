import time
from app import scheduler
from app.main import app

INTERVAL = 15  # seconds

if __name__ == "__main__":
    with app.app_context():
        while True:
            scheduler.tick()
            time.sleep(INTERVAL)