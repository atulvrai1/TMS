import sqlite3
from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller.compliance_reminders import generate_expiry_notifications


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE compliance_documents (
            document_id INTEGER PRIMARY KEY,
            owner_user_id INTEGER,
            document_type TEXT,
            expiry_date TEXT,
            truck_id INTEGER,
            driver_id INTEGER,
            trip_id INTEGER
        );
        CREATE TABLE trucks (truck_id INTEGER PRIMARY KEY, registration_number TEXT);
        CREATE TABLE drivers (driver_id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE trips (trip_id INTEGER PRIMARY KEY);
        INSERT INTO trucks VALUES (1, 'MH12AB1234');
        INSERT INTO compliance_documents
            (document_id, owner_user_id, document_type, expiry_date, truck_id)
            VALUES (1, 7, 'PUC Certificate', '2026-11-20', 1);
    """)
    return conn


def test_reminder_created_at_threshold_and_deduplicated():
    conn = make_db()
    today = date(2026, 10, 21)
    assert generate_expiry_notifications(conn, today) == 1
    assert generate_expiry_notifications(conn, today) == 0
    row = conn.execute("SELECT * FROM compliance_notifications").fetchone()
    assert row["alert_key"] == "due_30"
    assert "MH12AB1234" in row["message"]
    conn.close()


def test_overdue_reminder_created_once():
    conn = make_db()
    conn.execute("UPDATE compliance_documents SET expiry_date='2026-10-20'")
    conn.commit()
    today = date(2026, 10, 21)
    assert generate_expiry_notifications(conn, today) == 1
    assert generate_expiry_notifications(conn, today) == 0
    row = conn.execute("SELECT * FROM compliance_notifications").fetchone()
    assert row["alert_key"] == "overdue"
    assert "expired 1 day(s) ago" in row["message"]
    conn.close()


def test_renewal_suppresses_old_document_reminders():
    conn = make_db()
    conn.execute("""
        INSERT INTO compliance_documents
            (document_id, owner_user_id, document_type, expiry_date, truck_id)
        VALUES (2, 7, 'PUC Certificate', '2027-10-21', 1)
    """)
    conn.commit()
    # The old certificate is 30 days from expiry, but the renewed record is current.
    assert generate_expiry_notifications(conn, date(2026, 10, 21)) == 0
    conn.close()
