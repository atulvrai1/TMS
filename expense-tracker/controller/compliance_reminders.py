"""Persistent, deduplicated in-app expiry reminders for compliance documents."""
from datetime import date

REMINDER_DAYS = (30, 15, 7, 1, 0)


def ensure_notification_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS compliance_notifications (
            notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES compliance_documents(document_id) ON DELETE CASCADE,
            owner_user_id INTEGER,
            alert_key TEXT NOT NULL,
            expiry_date DATE NOT NULL,
            message TEXT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(document_id, expiry_date, alert_key)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_compliance_notifications_owner "
        "ON compliance_notifications(owner_user_id, created_at DESC)"
    )
    conn.commit()


def generate_expiry_notifications(conn, today=None):
    """Create one persistent reminder per expiry threshold; safe to run repeatedly."""
    today = today or date.today()
    ensure_notification_schema(conn)
    rows = conn.execute("""
        SELECT d.document_id, d.owner_user_id, d.document_type, d.expiry_date,
               t.registration_number, dr.name AS driver_name,
               tr.trip_id AS related_trip_id
        FROM compliance_documents d
        LEFT JOIN trucks t ON t.truck_id = d.truck_id
        LEFT JOIN drivers dr ON dr.driver_id = d.driver_id
        LEFT JOIN trips tr ON tr.trip_id = d.trip_id
        WHERE d.expiry_date IS NOT NULL
          AND d.document_id = (
              SELECT MAX(d2.document_id) FROM compliance_documents d2
              WHERE d2.document_type = d.document_type
                AND IFNULL(d2.truck_id, 0) = IFNULL(d.truck_id, 0)
                AND IFNULL(d2.driver_id, 0) = IFNULL(d.driver_id, 0)
                AND IFNULL(d2.trip_id, 0) = IFNULL(d.trip_id, 0)
                AND IFNULL(d2.owner_user_id, -1) = IFNULL(d.owner_user_id, -1)
          )
    """).fetchall()
    created = 0
    for row in rows:
        try:
            expiry = date.fromisoformat(str(row["expiry_date"]))
        except (TypeError, ValueError):
            continue
        days_left = (expiry - today).days
        if days_left in REMINDER_DAYS:
            alert_key = f"due_{days_left}"
        elif days_left < 0:
            alert_key = "overdue"
        else:
            continue

        subject = row["registration_number"] or row["driver_name"]
        if not subject and row["related_trip_id"]:
            subject = f"Trip #{row['related_trip_id']}"
        subject = subject or "Assigned record"
        if days_left < 0:
            message = f"{row['document_type']} for {subject} expired {-days_left} day(s) ago."
        elif days_left == 0:
            message = f"{row['document_type']} for {subject} expires today."
        else:
            message = f"{row['document_type']} for {subject} expires in {days_left} day(s)."

        result = conn.execute("""
            INSERT OR IGNORE INTO compliance_notifications
                (document_id, owner_user_id, alert_key, expiry_date, message)
            VALUES (?, ?, ?, ?, ?)
        """, (row["document_id"], row["owner_user_id"], alert_key, expiry.isoformat(), message))
        created += max(result.rowcount, 0)
    conn.commit()
    return created
