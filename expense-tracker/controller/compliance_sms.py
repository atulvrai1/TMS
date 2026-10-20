"""Fast2SMS integration for scheduled FleetFlow compliance-expiry reminders.

Domestic production SMS must use an operator-approved DLT header and content template.
The configured message template must match the approved template text exactly.
"""
import json
import logging
import os
import re
import time
from datetime import date, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import abort, flash, redirect, request, url_for

logger = logging.getLogger(__name__)
FAST2SMS_ENDPOINT = "https://www.fast2sms.com/dev/bulkV2"


def ensure_sms_schema(get_db):
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS compliance_sms_settings (
                setting_id INTEGER PRIMARY KEY CHECK (setting_id = 1),
                recipient_mobile TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_by INTEGER
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO compliance_sms_settings (setting_id, recipient_mobile, enabled)
            VALUES (1, '', 0)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS compliance_sms_log (
                sms_id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                recipient_mobile TEXT NOT NULL,
                milestone TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                provider_request_id TEXT,
                response_message TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(document_id, recipient_mobile, milestone)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_compliance_sms_log_status ON compliance_sms_log(status, last_attempt_at)")
        conn.commit()
    finally:
        conn.close()


def _normalise_mobile(raw):
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if len(digits) != 10 or digits[0] not in "6789":
        raise ValueError("Enter a valid Indian 10-digit mobile number.")
    return digits


def _send_fast2sms(mobile, message):
    api_key = os.environ.get("FAST2SMS_API_KEY", "").strip()
    sender_id = os.environ.get("FAST2SMS_SENDER_ID", "").strip()
    entity_id = os.environ.get("FAST2SMS_ENTITY_ID", "").strip()
    template_id = os.environ.get("FAST2SMS_TEMPLATE_ID", "").strip()
    if not all((api_key, sender_id, entity_id, template_id)):
        raise RuntimeError(
            "Fast2SMS is not fully configured. Set FAST2SMS_API_KEY, "
            "FAST2SMS_SENDER_ID, FAST2SMS_ENTITY_ID and FAST2SMS_TEMPLATE_ID."
        )
    payload = {
        "route": "dlt_manual",
        "sender_id": sender_id,
        "message": message,
        "template_id": template_id,
        "entity_id": entity_id,
        "numbers": mobile,
        "sms_details": "1",
    }
    req = Request(
        FAST2SMS_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Fast2SMS HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Fast2SMS request failed: {exc}") from exc
    if not result.get("return"):
        raise RuntimeError(str(result.get("message") or result)[:500])
    return str(result.get("request_id") or ""), str(result.get("message") or "Accepted")[:500]


def _document_label(row):
    if row["subject_type"] == "truck":
        subject = row["registration_number"] or f"truck #{row['truck_id']}"
    elif row["subject_type"] == "driver":
        subject = row["driver_name"] or f"driver #{row['driver_id']}"
    else:
        subject = f"trip #{row['trip_id']}"
    return subject


def _render_message(row, days_remaining):
    template = os.environ.get(
        "FAST2SMS_MESSAGE_TEMPLATE",
        "FleetFlow alert: {document} for {vehicle}. Please check and renew the compliance document.",
    )
    document = row["document_type"]
    vehicle = _document_label(row)
    # Keep values safe for a single SMS template variable and avoid injecting line breaks.
    document = re.sub(r"[\r\n|]+", " ", document)[:100]
    vehicle = re.sub(r"[\r\n|]+", " ", vehicle)[:100]
    if days_remaining > 0:
        vehicle = f"{vehicle}; expires in {days_remaining} days on {row['expiry_date']}"
    elif days_remaining == 0:
        vehicle = f"{vehicle}; expires today ({row['expiry_date']})"
    else:
        vehicle = f"{vehicle}; expired {abs(days_remaining)} day(s) ago ({row['expiry_date']})"
    try:
        message = template.format(document=document, vehicle=vehicle)
    except (KeyError, ValueError) as exc:
        raise RuntimeError("FAST2SMS_MESSAGE_TEMPLATE must use only {document} and {vehicle} placeholders.") from exc
    if len(message) > 500:
        raise RuntimeError("The configured SMS template expands beyond 500 characters.")
    return message


def _due_documents(conn):
    # The newest record for each document/asset pairing represents the current renewal.
    return conn.execute("""
        SELECT d.document_id, d.subject_type, d.document_type, d.expiry_date,
               d.truck_id, d.driver_id, d.trip_id,
               t.registration_number, dr.name AS driver_name,
               CAST(julianday(d.expiry_date) - julianday(date('now')) AS INTEGER) AS days_remaining
        FROM compliance_documents d
        LEFT JOIN trucks t ON t.truck_id = d.truck_id
        LEFT JOIN drivers dr ON dr.driver_id = d.driver_id
        WHERE d.expiry_date IS NOT NULL
          AND CAST(julianday(d.expiry_date) - julianday(date('now')) AS INTEGER) IN (30, 7, 0, -1)
          AND d.document_id = (
              SELECT MAX(d2.document_id) FROM compliance_documents d2
              WHERE d2.subject_type = d.subject_type
                AND d2.document_type = d.document_type
                AND IFNULL(d2.truck_id, 0) = IFNULL(d.truck_id, 0)
                AND IFNULL(d2.driver_id, 0) = IFNULL(d.driver_id, 0)
                AND IFNULL(d2.trip_id, 0) = IFNULL(d.trip_id, 0)
          )
        ORDER BY d.expiry_date, d.document_id
    """).fetchall()


def send_due_compliance_sms(get_db):
    """Send today's due reminders; successful submissions are deduplicated by document/milestone."""
    conn = get_db()
    sent = skipped = failed = 0
    try:
        settings = conn.execute(
            "SELECT recipient_mobile, enabled FROM compliance_sms_settings WHERE setting_id=1"
        ).fetchone()
        if not settings or not settings["enabled"] or not settings["recipient_mobile"]:
            return {"sent": 0, "skipped": 0, "failed": 0, "reason": "SMS notifications are disabled or no mobile number is configured."}
        try:
            mobile = _normalise_mobile(settings["recipient_mobile"])
        except ValueError as exc:
            return {"sent": 0, "skipped": 0, "failed": 0, "reason": str(exc)}

        for row in _due_documents(conn):
            days = int(row["days_remaining"])
            milestone = {30: "30_days", 7: "7_days", 0: "expiry_day", -1: "1_day_overdue"}[days]
            conn.execute(
                "INSERT OR IGNORE INTO compliance_sms_log "
                "(document_id, recipient_mobile, milestone, status) VALUES (?, ?, ?, 'pending')",
                (row["document_id"], mobile, milestone),
            )
            log = conn.execute(
                "SELECT sms_id, status FROM compliance_sms_log "
                "WHERE document_id=? AND recipient_mobile=? AND milestone=?",
                (row["document_id"], mobile, milestone),
            ).fetchone()
            if log["status"] in ("submitted", "delivered"):
                skipped += 1
                continue
            conn.execute(
                "UPDATE compliance_sms_log SET status='pending', attempts=attempts+1, "
                "last_attempt_at=CURRENT_TIMESTAMP WHERE sms_id=?",
                (log["sms_id"],),
            )
            conn.commit()
            try:
                message = _render_message(row, days)
                request_id, response_message = _send_fast2sms(mobile, message)
                conn.execute(
                    "UPDATE compliance_sms_log SET status='submitted', provider_request_id=?, "
                    "response_message=? WHERE sms_id=?",
                    (request_id, response_message, log["sms_id"]),
                )
                conn.commit()
                sent += 1
            except Exception as exc:
                logger.exception("Compliance SMS failed for document_id=%s", row["document_id"])
                conn.execute(
                    "UPDATE compliance_sms_log SET status='failed', response_message=? WHERE sms_id=?",
                    (str(exc)[:500], log["sms_id"]),
                )
                conn.commit()
                failed += 1
        return {"sent": sent, "skipped": skipped, "failed": failed, "reason": None}
    finally:
        conn.close()


def register_compliance_sms_module(app, ns):
    get_db = ns["get_db"]
    admin_required = ns["admin_required"]
    valid_csrf = ns["_valid_csrf_token"]
    ensure_sms_schema(get_db)

    @app.route("/admin/notifications/sms-settings", methods=["POST"], endpoint="compliance_sms_settings_save")
    @admin_required
    def compliance_sms_settings_save():
        if not valid_csrf():
            flash("Your session token is invalid. Reload the page and try again.", "error")
            return redirect(url_for("admin_notifications"))
        try:
            mobile = _normalise_mobile(request.form.get("recipient_mobile", ""))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin_notifications"))
        enabled = 1 if request.form.get("enabled") == "1" else 0
        conn = get_db()
        try:
            conn.execute(
                "UPDATE compliance_sms_settings SET recipient_mobile=?, enabled=?, "
                "updated_at=CURRENT_TIMESTAMP, updated_by=? WHERE setting_id=1",
                (mobile, enabled, ns["session"].get("user_id")),
            )
            conn.commit()
        finally:
            conn.close()
        flash("SMS notification settings saved. A Fast2SMS DLT configuration is still required before delivery.", "success")
        return redirect(url_for("admin_notifications"))

    @app.route("/admin/notifications/sms-test", methods=["POST"], endpoint="compliance_sms_test")
    @admin_required
    def compliance_sms_test():
        if not valid_csrf():
            flash("Your session token is invalid. Reload the page and try again.", "error")
            return redirect(url_for("admin_notifications"))
        conn = get_db()
        try:
            settings = conn.execute(
                "SELECT recipient_mobile, enabled FROM compliance_sms_settings WHERE setting_id=1"
            ).fetchone()
            if not settings or not settings["recipient_mobile"]:
                flash("Save a mobile number first.", "error")
                return redirect(url_for("admin_notifications"))
            if not settings["enabled"]:
                flash("Enable SMS notifications and save settings before sending a test.", "error")
                return redirect(url_for("admin_notifications"))
            mobile = _normalise_mobile(settings["recipient_mobile"])
        finally:
            conn.close()
        try:
            sample = "FleetFlow test: compliance document reminder setup. Please check your FleetFlow notifications."
            _send_fast2sms(mobile, sample)
            flash("Fast2SMS accepted the test message. This confirms API submission, not handset delivery.", "success")
        except Exception as exc:
            logger.exception("Fast2SMS test failed")
            flash(f"Test SMS failed: {str(exc)[:220]}", "error")
        return redirect(url_for("admin_notifications"))
