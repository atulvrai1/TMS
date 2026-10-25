"""Regression coverage for the trip-linked loading slip and POD workflow."""
import os
import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_loading_slip_creation_and_pod_status(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_PATH": str(tmp_path / "transport-documents.db"),
        "FLASK_SECRET_KEY": "test-only-secret-key-that-is-at-least-32-characters-long",
        "APP_ENV": "test",
        "PYTHONPATH": str(PROJECT_DIR),
        "FLEETFLOW_SUPER_ADMIN_EMAIL": "root@example.test",
    })
    code = r"""
from datetime import date
from app import app
from database.db import get_db, init_db

init_db()
conn = get_db()
user_id = conn.execute(
    "INSERT INTO users (email, password, full_name, is_approved, role, job_role) "
    "VALUES (?, ?, ?, 1, 'Admin', 'Admin')",
    ("docs-admin@example.test", "not-used", "Documents Admin"),
).lastrowid
truck_id = conn.execute(
    "INSERT INTO trucks (registration_number, model, year, status) "
    "VALUES (?, ?, ?, 'Available')",
    ("TEST-DOC-001", "Test Truck", 2024),
).lastrowid
driver_id = conn.execute(
    "INSERT INTO drivers (name, license_number, status) VALUES (?, ?, 'Active')",
    ("Test Driver", "TEST-LIC-001"),
).lastrowid
trip_id = conn.execute(
    "INSERT INTO trips (truck_id, driver_id, freight_amount, start_date, route_from, route_to, status, owner_user_id) "
    "VALUES (?, ?, 25000, ?, 'Patna', 'Indore', 'Scheduled', ?)",
    (truck_id, driver_id, date.today().isoformat(), user_id),
).lastrowid
conn.commit()
conn.close()

with app.test_client() as client:
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["email"] = "docs-admin@example.test"
        session["role"] = "Admin"
        session["is_admin"] = True
        session["csrf_token"] = "docs-test-csrf"

    response = client.get("/transport-documents")
    assert response.status_code == 200, response.status_code
    assert b"Loading Slips & Proof of Delivery" in response.data

    response = client.post("/transport-documents", data={
        "csrf_token": "docs-test-csrf",
        "action": "create",
        "trip_id": str(trip_id),
        "document_type": "Loading Slip",
        "document_number": "LR-TEST-001",
        "document_date": date.today().isoformat(),
        "cargo_description": "Packaged goods",
        "quantity": "12 tonnes",
        "notes": "Handle with care",
    })
    assert response.status_code == 302, response.status_code

    conn = get_db()
    document = conn.execute(
        "SELECT document_id, status FROM transport_documents WHERE document_number = ?",
        ("LR-TEST-001",),
    ).fetchone()
    assert document is not None
    assert document["status"] == "Draft"
    conn.close()

    response = client.post("/transport-documents", data={
        "csrf_token": "docs-test-csrf",
        "action": "update_status",
        "document_id": str(document["document_id"]),
        "status": "POD Received",
        "pod_reference": "POD-TEST-001",
    })
    assert response.status_code == 302, response.status_code

    conn = get_db()
    saved = conn.execute(
        "SELECT status, pod_reference FROM transport_documents WHERE document_id = ?",
        (document["document_id"],),
    ).fetchone()
    assert saved["status"] == "POD Received"
    assert saved["pod_reference"] == "POD-TEST-001"
    conn.close()
"""
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_DIR,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
