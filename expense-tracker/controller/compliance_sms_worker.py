"""Daily worker for WhatsApp compliance reminders. Runs separately from Gunicorn workers."""
import logging
import os
import time

# Importing app initializes the database and registers compliance tables/routes.
import app as fleetflow_app
from database.db import get_db
from controller.compliance_sms import send_due_compliance_whatsapp

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("fleetflow.compliance_sms_worker")


def run_once():
    result = send_due_compliance_whatsapp(get_db)
    logger.info("Compliance WhatsApp check result: %s", result)
    return result


if __name__ == "__main__":
    interval = max(3600, int(os.environ.get("COMPLIANCE_SMS_CHECK_INTERVAL_SECONDS", "86400")))
    logger.info("Compliance WhatsApp worker started; interval=%s seconds", interval)
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("Compliance SMS worker iteration failed")
        time.sleep(interval)
