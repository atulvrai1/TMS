"""Regression coverage for scoped analytics and chart data rendering."""
import os
import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_reports_dashboard_renders_scoped_metrics_and_charts(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_PATH": str(tmp_path / "analytics.db"),
        "FLASK_SECRET_KEY": "test-only-secret-key-that-is-at-least-32-characters-long",
        "APP_ENV": "test",
        "PYTHONPATH": str(PROJECT_DIR),
        "FLEETFLOW_SUPER_ADMIN_EMAIL": "root@example.test",
    })
    code = r"""
from datetime import date, timedelta
from app import app
from database.db import get_db

conn = get_db()
user_id = conn.execute(
    "INSERT INTO users (email, password, full_name, is_approved, role, job_role) "
    "VALUES (?, ?, ?, 1, 'Admin', 'Admin')",
    ("analytics-admin@example.test", "not-used", "Analytics Admin"),
).lastrowid
truck_id = conn.execute(
    "INSERT INTO trucks (registration_number, model, year, status) "
    "VALUES (?, ?, ?, 'Available')",
    ("ANALYTICS-001", "Test Truck", 2024),
).lastrowid
driver_id = conn.execute(
    "INSERT INTO drivers (name, license_number, status) VALUES (?, ?, 'Active')",
    ("Analytics Driver", "ANALYTICS-LIC-001"),
).lastrowid
trip_id = conn.execute(
    "INSERT INTO trips (truck_id, driver_id, freight_amount, start_date, route_from, route_to, status, owner_user_id) "
    "VALUES (?, ?, 50000, ?, 'Patna', 'Indore', 'Completed', ?)",
    (truck_id, driver_id, date.today().isoformat(), user_id),
).lastrowid
older_trip_id = conn.execute(
    "INSERT INTO trips (truck_id, driver_id, freight_amount, start_date, route_from, route_to, status, owner_user_id) "
    "VALUES (?, ?, 30000, ?, 'Patna', 'Delhi', 'Completed', ?)",
    (truck_id, driver_id, (date.today() - timedelta(days=1)).isoformat(), user_id),
).lastrowid
newer_same_day_trip_id = conn.execute(
    "INSERT INTO trips (truck_id, driver_id, freight_amount, start_date, route_from, route_to, status, owner_user_id) "
    "VALUES (?, ?, 60000, ?, 'Patna', 'Ranchi', 'Completed', ?)",
    (truck_id, driver_id, date.today().isoformat(), user_id),
).lastrowid
conn.execute(
    "INSERT INTO expenses (trip_id, category, amount, date, description, created_by, status) "
    "VALUES (?, 'Fuel', 7500, ?, 'Diesel', ?, 'Approved')",
    (trip_id, date.today().isoformat(), user_id),
)
conn.execute(
    "INSERT INTO trip_payments (trip_id, amount, payment_date, reference, recorded_by) "
    "VALUES (?, 20000, ?, 'RCPT-ANALYTICS-1', ?)",
    (trip_id, date.today().isoformat(), user_id),
)
conn.commit()
conn.close()

with app.test_client() as client:
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["email"] = "analytics-admin@example.test"
        session["role"] = "Admin"
        session["is_admin"] = True
        session["csrf_token"] = "analytics-test-csrf"

    response = client.get(
        "/reports?start_date=" + date.today().isoformat() +
        "&end_date=" + date.today().isoformat()
    )
    assert response.status_code == 200, response.status_code
    assert b"Fleet & Business Intelligence" in response.data
    assert b"Latest freight trips" in response.data
    assert b"Top freight trips" not in response.data
    # Latest trip date first; for equal dates, the larger trip ID comes first.
    row_newest = response.data.index(f"#{newer_same_day_trip_id}".encode())
    row_original = response.data.index(f"#{trip_id}".encode())
    row_older = response.data.index(f"#{older_trip_id}".encode())
    assert row_newest < row_original < row_older
    assert b"Booked freight" in response.data
    assert b"Approved costs by category" in response.data
    assert b"50000.00" in response.data
    assert b"20000.00" in response.data
    assert b"7500.00" in response.data
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
        f"Analytics workflow failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert (tmp_path / "analytics.db").exists()
