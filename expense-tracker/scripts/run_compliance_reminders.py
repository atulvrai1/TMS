"""Run once per day from cron/systemd to persist expiry notifications."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import app  # noqa: E402
from database.db import get_db  # noqa: E402
from controller.compliance_reminders import generate_expiry_notifications  # noqa: E402


def main():
    with app.app_context():
        conn = get_db()
        try:
            created = generate_expiry_notifications(conn)
            print(f"Compliance reminders created: {created}")
        finally:
            conn.close()


if __name__ == "__main__":
    main()
