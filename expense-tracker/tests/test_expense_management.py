"""Expense workflow smoke tests with an isolated SQLite database."""
import os
import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_admin_receipts_filters_csv_and_driver_review(tmp_path):
    database_path = tmp_path / "expense-test-tms.db"
    upload_path = tmp_path / "private-uploads"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_PATH": str(database_path),
            "FLEETFLOW_UPLOAD_DIR": str(upload_path),
            "FLASK_SECRET_KEY": "test-only-secret-key-that-is-at-least-32-characters-long",
            "APP_ENV": "test",
            "PYTHONPATH": str(PROJECT_DIR),
        }
    )
    code = r"""
from io import BytesIO
from app import app
from database.db import get_db
from werkzeug.security import generate_password_hash

conn = get_db()
password = generate_password_hash("Correct-Horse-123!")
admin_id = conn.execute(
    "INSERT INTO users (email, password, full_name, is_approved, is_rejected, "
    "is_terminated, role, job_role) VALUES (?, ?, ?, 1, 0, 0, 'Admin', 'Admin')",
    ("admin@example.com", password, "Fleet Admin"),
).lastrowid
other_admin_id = conn.execute(
    "INSERT INTO users (email, password, full_name, is_approved, is_rejected, "
    "is_terminated, role, job_role) VALUES (?, ?, ?, 1, 0, 0, 'Admin', 'Admin')",
    ("other-admin@example.com", password, "Other Admin"),
).lastrowid
driver_id = conn.execute(
    "INSERT INTO drivers (name, license_number, status) VALUES (?, ?, 'Active')",
    ("Test Driver", "TEST-LIC-001"),
).lastrowid
driver_user_id = conn.execute(
    "INSERT INTO users (email, password, full_name, is_approved, is_rejected, "
    "is_terminated, role, job_role, driver_id) "
    "VALUES (?, ?, ?, 1, 0, 0, 'User', 'User', ?)",
    ("driver@example.com", password, "Test Driver User", driver_id),
).lastrowid
truck_id = conn.execute(
    "INSERT INTO trucks (registration_number, model, year, status) "
    "VALUES (?, ?, 2024, 'Available')",
    ("TEST-TRUCK-001", "Test truck"),
).lastrowid
trip_id = conn.execute(
    "INSERT INTO trips (truck_id, driver_id, start_date, route_from, route_to, "
    "freight_amount, status, owner_user_id) "
    "VALUES (?, ?, '2026-10-01', 'Indore', 'Bhopal', 25000, 'Completed', ?)",
    (truck_id, driver_id, other_admin_id),
).lastrowid
conn.commit()
conn.close()

with app.test_client() as admin_client:
    with admin_client.session_transaction() as session:
        session["user_id"] = admin_id
        session["csrf_token"] = "admin-csrf-token"

    # An Admin must see trips owned by another Admin in the shared fleet ledger.
    page = admin_client.get("/expenses")
    assert page.status_code == 200, page.status_code
    assert b"Indore" in page.data and b"Bhopal" in page.data, page.data.decode()

    create_response = admin_client.post(
        "/expenses/create",
        data={
            "csrf_token": "admin-csrf-token",
            "trip_id": str(trip_id),
            "category": "Fuel",
            "amount": "1250.50",
            "date": "2026-10-02",
            "description": "Diesel bill",
            "receipt": (BytesIO(b"%PDF-1.7 test receipt"), "fuel-bill.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert create_response.status_code == 302, create_response.status_code

    conn = get_db()
    expense = conn.execute(
        "SELECT expense_id, status, receipt_storage_name, receipt_original_name, receipt_size "
        "FROM expenses WHERE category = 'Fuel'"
    ).fetchone()
    assert expense is not None
    assert expense["status"] == "Approved"
    assert expense["receipt_storage_name"]
    assert expense["receipt_original_name"] == "fuel-bill.pdf"
    expense_id = expense["expense_id"]
    conn.close()

    receipt_response = admin_client.get(f"/expenses/{expense_id}/receipt")
    assert receipt_response.status_code == 200, receipt_response.status_code
    assert receipt_response.data.startswith(b"%PDF-")

    export_response = admin_client.get(
        "/expenses/export.csv?category=Fuel&date_from=2026-10-01&date_to=2026-10-31"
    )
    assert export_response.status_code == 200
    assert b"fuel-bill.pdf" in export_response.data
    assert b"Review Note / Rejection Reason" in export_response.data

with app.test_client() as driver_client:
    with driver_client.session_transaction() as session:
        session["user_id"] = driver_user_id
        session["csrf_token"] = "driver-csrf-token"

    submit_response = driver_client.post(
        "/expenses/create",
        data={
            "csrf_token": "driver-csrf-token",
            "trip_id": str(trip_id),
            "category": "Toll",
            "amount": "300",
            "date": "2026-10-03",
            "description": "Highway toll",
        },
    )
    assert submit_response.status_code == 302, submit_response.status_code

conn = get_db()
pending = conn.execute(
    "SELECT expense_id, status FROM expenses WHERE category = 'Toll'"
).fetchone()
assert pending is not None and pending["status"] == "Pending"
pending_id = pending["expense_id"]
conn.close()

with app.test_client() as admin_client:
    with admin_client.session_transaction() as session:
        session["user_id"] = admin_id
        session["csrf_token"] = "admin-review-token"

    review_response = admin_client.post(
        f"/expenses/{pending_id}/review",
        data={
            "csrf_token": "admin-review-token",
            "status": "Rejected",
            "review_note": "Missing toll receipt",
        },
    )
    assert review_response.status_code == 302, review_response.status_code

conn = get_db()
rejected = conn.execute(
    "SELECT status, review_note FROM expenses WHERE expense_id = ?",
    (pending_id,),
).fetchone()
assert rejected["status"] == "Rejected"
assert rejected["review_note"] == "Missing toll receipt"
conn.close()

with app.test_client() as admin_client:
    with admin_client.session_transaction() as session:
        session["user_id"] = admin_id
        session["csrf_token"] = "admin-final-token"
    filtered_page = admin_client.get("/expenses?status=Rejected&category=Toll")
    assert filtered_page.status_code == 200
    assert b"Missing toll receipt" in filtered_page.data
    csv_response = admin_client.get("/expenses/export.csv?status=Rejected")
    assert csv_response.status_code == 200
    assert b"Missing toll receipt" in csv_response.data
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
        "Expense workflow test failed.\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert database_path.exists()
