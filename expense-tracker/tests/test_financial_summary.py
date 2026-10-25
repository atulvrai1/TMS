"""Regression tests for fleet financial totals across trip statuses."""
import os
import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_financial_summary_recognizes_revenue_but_keeps_active_trip_costs(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_PATH": str(tmp_path / "financials.db"),
        "FLASK_SECRET_KEY": "test-only-secret-key-that-is-at-least-32-characters-long",
        "APP_ENV": "test",
        "PYTHONPATH": str(PROJECT_DIR),
        "FLEETFLOW_SUPER_ADMIN_EMAIL": "root@example.test",
    })
    code = r"""
from datetime import date
from app import app, _get_trip_financials
from database.db import get_db

conn = get_db()
user_id = conn.execute(
    "INSERT INTO users (email, password, full_name, is_approved, role, job_role) "
    "VALUES (?, ?, ?, 1, 'Admin', 'Admin')",
    ("finance-test@example.test", "not-used", "Finance Test"),
).lastrowid
truck_id = conn.execute(
    "INSERT INTO trucks (registration_number, model, year, status) VALUES (?, ?, ?, 'Available')",
    ("FIN-TEST-001", "Test Truck", 2024),
).lastrowid
driver_id = conn.execute(
    "INSERT INTO drivers (name, license_number, status) VALUES (?, ?, 'Active')",
    ("Finance Driver", "FIN-TEST-LIC"),
).lastrowid

def trip(status, freight, route):
    return conn.execute(
        "INSERT INTO trips (truck_id, driver_id, freight_amount, start_date, route_from, route_to, status, owner_user_id) "
        "VALUES (?, ?, ?, ?, 'Patna', ?, ?, ?)",
        (truck_id, driver_id, freight, date.today().isoformat(), route, status, user_id),
    ).lastrowid

completed = trip("Completed", 620000, "Indore")
active = trip("In Progress", 20000, "Ranchi")
cancelled = trip("Cancelled", 10000, "Delhi")

def expense(trip_id, amount, status, suffix):
    conn.execute(
        "INSERT INTO expenses (trip_id, category, amount, date, description, created_by, status) "
        "VALUES (?, 'Fuel', ?, ?, ?, ?, ?)",
        (trip_id, amount, date.today().isoformat(), suffix, user_id, status),
    )

expense(completed, 40000, "Approved", "completed cost")
expense(active, 500, "Approved", "active cost")
expense(active, 250, "Pending", "pending active cost")
expense(cancelled, 100, "Approved", "cancelled trip cost")
conn.execute(
    "INSERT INTO trip_payments (trip_id, amount, payment_date, reference, recorded_by) "
    "VALUES (?, 620000, ?, 'FIN-COMPLETE', ?)",
    (completed, date.today().isoformat(), user_id),
)
conn.execute(
    "INSERT INTO trip_payments (trip_id, amount, payment_date, reference, recorded_by) "
    "VALUES (?, 5000, ?, 'FIN-ACTIVE', ?)",
    (active, date.today().isoformat(), user_id),
)
conn.commit()

summary, rows = _get_trip_financials(conn, user_id)
assert summary["freight_total"] == 620000
assert summary["received_total"] == 625000
assert summary["balance_due_total"] == 0
assert summary["approved_expenses_total"] == 40600
assert summary["pending_expenses_total"] == 250
assert summary["profit_loss_total"] == 579400
assert summary["cash_profit_loss_total"] == 584400
assert len(rows) == 3
assert next(row for row in rows if row["trip_id"] == active)["recognized_freight"] == 0
conn.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, (
        f"Financial regression test failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
