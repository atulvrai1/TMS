"""Official WhatsApp Cloud API integration for FleetFlow compliance reminders."""
import json
import logging
import os
import re
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import flash, redirect, request, url_for

logger = logging.getLogger(__name__)
DEFAULT_GRAPH_API_VERSION = "v23.0"
DEFAULT_TEMPLATE_NAME = "fleetflow_compliance_alert"


def ensure_sms_schema(get_db):
    """Create persistent settings and deduplication tables (legacy table names retained)."""
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
            INSERT OR IGNORE INTO compliance_sms_settings
                (setting_id, recipient_mobile, enabled) VALUES (1, '', 0)
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_compliance_sms_log_status "
            "ON compliance_sms_log(status, last_attempt_at)"
        )
        conn.commit()
    finally:
        conn.close()


def _normalise_mobile(raw):
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if len(digits) != 10 or digits[0] not in "6789":
        raise ValueError("Enter a valid Indian 10-digit WhatsApp number.")
    return digits


def _document_label(row):
    if row["subject_type"] == "truck":
        return row["registration_number"] or f"truck #{row['truck_id']}"
    if row["subject_type"] == "driver":
        return row["driver_name"] or f"driver #{row['driver_id']}"
    return f"trip #{row['trip_id']}"


def _expiry_status(days_remaining, expiry_date):
    if days_remaining > 0:
        return f"Expires in {days_remaining} days on {expiry_date}"
    if days_remaining == 0:
        return f"Expires today ({expiry_date})"
    return f"Expired {abs(days_remaining)} day(s) ago ({expiry_date})"


def _template_parameters(row, days_remaining):
    """Return the three text variables required by the approved WhatsApp template."""
    return [
        re.sub(r"[\r\n]+", " ", str(row["document_type"]))[:200],
        re.sub(r"[\r\n]+", " ", _document_label(row))[:200],
        _expiry_status(days_remaining, row["expiry_date"])[:200],
    ]


def _send_whatsapp_template(mobile, parameters):
    """Send an approved utility template using Meta's official WhatsApp Cloud API."""
    access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    api_version = os.environ.get("WHATSAPP_API_VERSION", DEFAULT_GRAPH_API_VERSION).strip()
    template_name = os.environ.get("WHATSAPP_TEMPLATE_NAME", DEFAULT_TEMPLATE_NAME).strip()
    language_code = os.environ.get("WHATSAPP_TEMPLATE_LANGUAGE", "en").strip() or "en"

    if not access_token or not phone_number_id:
        raise RuntimeError(
            "WhatsApp Cloud API is not configured. Set WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID."
        )
    if not re.fullmatch(r"v\d+\.\d+", api_version):
        raise RuntimeError("WHATSAPP_API_VERSION must look like v23.0.")
    if not re.fullmatch(r"[a-z0-9_]+", template_name):
        raise RuntimeError("WHATSAPP_TEMPLATE_NAME must be a valid approved template name.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", phone_number_id):
        raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID is invalid.")

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": f"91{mobile}",
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": str(value)} for value in parameters],
            }],
        },
    }
    endpoint = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    req = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status_code = getattr(response, "status", 200)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"WhatsApp Cloud API HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"WhatsApp Cloud API request failed: {exc}") from exc

    if not 200 <= int(status_code) < 300:
        raise RuntimeError(f"WhatsApp Cloud API returned HTTP {status_code}: {raw[:300]}")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("WhatsApp Cloud API returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("WhatsApp Cloud API returned an unexpected response.")
    if result.get("error"):
        error = result["error"]
        raise RuntimeError(str(error.get("message") or error)[:500])
    messages = result.get("messages") or []
    message_id = messages[0].get("id", "") if messages and isinstance(messages[0], dict) else ""
    if not message_id:
        raise RuntimeError(f"WhatsApp Cloud API did not return a message ID: {str(result)[:300]}")
    return str(message_id)[:200], "Accepted by WhatsApp Cloud API; delivery status may arrive asynchronously."


def _due_documents(conn):
    """Latest record for each asset/document pair, at configured expiry milestones."""
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


def send_due_compliance_whatsapp(get_db):
    """Compatibility name: send WhatsApp reminders and deduplicate per document/milestone."""
    conn = get_db()
    sent = skipped = failed = 0
    try:
        settings = conn.execute(
            "SELECT recipient_mobile, enabled FROM compliance_sms_settings WHERE setting_id=1"
        ).fetchone()
        if not settings or not settings["enabled"] or not settings["recipient_mobile"]:
            return {"sent": 0, "skipped": 0, "failed": 0,
                    "reason": "WhatsApp notifications are disabled or no recipient number is configured."}
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
                parameters = _template_parameters(row, days)
                request_id, response_message = _send_whatsapp_template(mobile, parameters)
                conn.execute(
                    "UPDATE compliance_sms_log SET status='submitted', provider_request_id=?, "
                    "response_message=? WHERE sms_id=?",
                    (request_id, response_message, log["sms_id"]),
                )
                conn.commit()
                sent += 1
            except Exception as exc:
                logger.exception("Compliance WhatsApp failed for document_id=%s", row["document_id"])
                conn.execute(
                    "UPDATE compliance_sms_log SET status='failed', response_message=? WHERE sms_id=?",
                    (str(exc)[:500], log["sms_id"]),
                )
                conn.commit()
                failed += 1
        return {"sent": sent, "skipped": skipped, "failed": failed, "reason": None}
    finally:
        conn.close()


# Keep the old callable name for any local scripts that used the SMS worker API.
send_due_compliance_sms = send_due_compliance_whatsapp


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
        flash("WhatsApp notification settings saved. Configure the official WhatsApp Cloud API credentials before delivery.", "success")
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
                flash("Save a WhatsApp number first.", "error")
                return redirect(url_for("admin_notifications"))
            if not settings["enabled"]:
                flash("Enable WhatsApp notifications and save settings before sending a test.", "error")
                return redirect(url_for("admin_notifications"))
            mobile = _normalise_mobile(settings["recipient_mobile"])
        finally:
            conn.close()
        try:
            sample_row = {
                "document_type": "TEST DOCUMENT", "subject_type": "truck",
                "registration_number": "TEST-TRUCK", "truck_id": 0,
                "driver_id": None, "trip_id": None, "driver_name": None,
                "expiry_date": date.today().isoformat(),
            }
            _send_whatsapp_template(mobile, _template_parameters(sample_row, 7))
            flash("WhatsApp Cloud API accepted the test template. This confirms API submission, not final delivery.", "success")
        except Exception as exc:
            logger.exception("WhatsApp test message failed")
            flash(f"Test WhatsApp failed: {str(exc)[:220]}", "error")
        return redirect(url_for("admin_notifications"))
