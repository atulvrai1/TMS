"""End-to-end trip dispatch and finance checks using an isolated SQLite database."""
import os
import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_trip_dispatch_completion_and_payment_lifecycle(tmp_path):
    database_path = tmp_path / "trip-workflow.db"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_PATH": str(database_path),
            "FLASK_SECRET_KEY": "test-only-secret-key-that-is-at-least-32-characters-long",
            "APP_ENV": "test",
            "PYTHONPATH": str(PROJECT_DIR),
            "FLEETFLOW_SUPER_ADMIN_EMAIL": "root@example.test",
        }
    )
    code = r"""
from datetime import date
from app import app
from database.db import get_db

conn = get_db()
cursor = conn.execute(
    "INSERT INTO users (email, password, full_name, is_approved, role, job_role) "
    "VALUES (?, ?, ?, 1, 'Admin', 'Admin')",
    ("fleet-admin@example.test", "not-used-in-this-session", "Fleet Admin"),
)
admin_id = cursor.lastrowid
truck_id = conn.execute(
    "INSERT INTO trucks (registration_number, model, year, capacity_tons, status, owner_user_id) "
    "VALUES (?, ?, ?, ?, 'Available', ?)",
    ("TMS-TEST-001", "Test Truck", 2025, 20.0, admin_id),
).lastrowid
driver_id = conn.execute(
    "INSERT INTO drivers (name, license_number, contact_info, status, is_terminated) "
    "VALUES (?, ?, ?, 'Active', 0)",
    ("Test Driver", "TEST-LIC-001", "0000000000"),
).lastrowid
conn.commit()
conn.close()

with app.test_client() as client:
    with client.session_transaction() as session:
        session["user_id"] = admin_id
        session["csrf_token"] = "workflow-csrf-token"

    dashboard = client.get("/trips")
    assert dashboard.status_code == 200, dashboard.status_code
    assert b"mobile-menu-toggle" in dashboard.data
    assert b"Maintenance" in dashboard.data

    response = client.post("/trips/create", data={
        "csrf_token": "workflow-csrf-token",
        "route_from": "Indore",
        "route_to": "Mumbai",
        "start_date": date.today().isoformat(),
        "truck_id": str(truck_id),
        "driver_id": str(driver_id),
        "owner_user_id": str(admin_id),
        "freight_amount": "1000.00",
    })
    assert response.status_code == 302, response.status_code

    conn = get_db()
    trip = conn.execute(
        "SELECT trip_id, status FROM trips WHERE truck_id = ?",
        (truck_id,),
    ).fetchone()
    assert trip is not None and trip["status"] == "Scheduled"
    trip_id = trip["trip_id"]
    conn.close()

    response = client.post(f"/trips/{trip_id}/status", data={
        "csrf_token": "workflow-csrf-token",
        "status": "In Progress",
        "odometer": "100",
    })
    assert response.status_code == 302

    conn = get_db()
    trip = conn.execute("SELECT status, start_odometer FROM trips WHERE trip_id = ?", (trip_id,)).fetchone()
    truck = conn.execute("SELECT status FROM trucks WHERE truck_id = ?", (truck_id,)).fetchone()
    assert trip["status"] == "In Progress" and trip["start_odometer"] == 100
    assert truck["status"] == "In Trip"
    conn.close()

    # Invalid return odometer must not complete the trip or release the truck.
    client.post(f"/trips/{trip_id}/status", data={
        "csrf_token": "workflow-csrf-token",
        "status": "Completed",
        "odometer": "99",
    })
    conn = get_db()
    trip = conn.execute("SELECT status FROM trips WHERE trip_id = ?", (trip_id,)).fetchone()
    assert trip["status"] == "In Progress"
    conn.close()

    response = client.post(f"/trips/{trip_id}/status", data={
        "csrf_token": "workflow-csrf-token",
        "status": "Completed",
        "odometer": "150",
    })
    assert response.status_code == 302
    conn = get_db()
    trip = conn.execute("SELECT status, end_odometer, end_date FROM trips WHERE trip_id = ?", (trip_id,)).fetchone()
    truck = conn.execute("SELECT status FROM trucks WHERE truck_id = ?", (truck_id,)).fetchone()
    assert trip["status"] == "Completed" and trip["end_odometer"] == 150
    assert trip["end_date"] is not None and truck["status"] == "Available"
    conn.close()

    # Overpayment is rejected; a valid payment is recorded in the ledger.
    client.post(f"/trips/{trip_id}/payments", data={
        "csrf_token": "workflow-csrf-token",
        "amount": "1000.01",
        "payment_date": date.today().isoformat(),
        "reference": "overpayment-test",
    })
    conn = get_db()
    assert conn.execute("SELECT COUNT(*) FROM trip_payments WHERE trip_id = ?", (trip_id,)).fetchone()[0] == 0
    conn.close()

    client.post(f"/trips/{trip_id}/payments", data={
        "csrf_token": "workflow-csrf-token",
        "amount": "600.00",
        "payment_date": date.today().isoformat(),
        "reference": "receipt-test-001",
    })
    client.post(f"/trips/{trip_id}/payments", data={
        "csrf_token": "workflow-csrf-token",
        "amount": "400.00",
        "payment_date": date.today().isoformat(),
        "reference": "receipt-test-002",
    })
    conn = get_db()
    totals = conn.execute(
        "SELECT COUNT(*) AS payment_count, SUM(amount) AS amount_received "
        "FROM trip_payments WHERE trip_id = ?",
        (trip_id,),
    ).fetchone()
    assert totals["payment_count"] == 2
    assert round(totals["amount_received"], 2) == 1000.00
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
        f"Trip workflow test failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert database_path.exists()
