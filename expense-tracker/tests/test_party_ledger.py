"""Party register and ledger workflows against an isolated SQLite database."""
import os
import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_party_register_and_debit_credit_ledger(tmp_path):
    database_path = tmp_path / "party-ledger.db"
    env = os.environ.copy()
    env.update({
        "DATABASE_PATH": str(database_path),
        "FLASK_SECRET_KEY": "test-only-secret-key-that-is-at-least-32-characters-long",
        "APP_ENV": "test",
        "PYTHONPATH": str(PROJECT_DIR),
        "FLEETFLOW_SUPER_ADMIN_EMAIL": "root@example.test",
    })
    code = r"""
from datetime import date
from app import app
from database.db import get_db

conn = get_db()
cur = conn.execute(
    "INSERT INTO users (email, password, full_name, is_approved, role, job_role) "
    "VALUES (?, ?, ?, 1, 'Admin', 'Admin')",
    ("party-admin@example.test", "not-used", "Party Admin"),
)
user_id = cur.lastrowid
conn.commit()
conn.close()

with app.test_client() as client:
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["email"] = "party-admin@example.test"
        session["role"] = "Admin"
        session["is_admin"] = True
        session["csrf_token"] = "party-test-csrf"

    response = client.get("/parties")
    assert response.status_code == 200, response.status_code
    assert b"Parties & Ledger" in response.data

    response = client.post("/parties", data={
        "csrf_token": "party-test-csrf",
        "name": "Acme Logistics",
        "party_type": "Customer",
        "phone": "9876543210",
        "email": "accounts@example.test",
        "gstin": "",
        "address": "Indore",
        "notes": "Test customer",
    })
    assert response.status_code == 302
    conn = get_db()
    party = conn.execute("SELECT party_id FROM parties WHERE name = ?", ("Acme Logistics",)).fetchone()
    assert party is not None
    party_id = party["party_id"]
    conn.close()

    response = client.post(f"/parties/{party_id}", data={
        "csrf_token": "party-test-csrf",
        "entry_date": date.today().isoformat(),
        "entry_type": "Debit",
        "amount": "12500.50",
        "reference": "INV-001",
        "description": "Freight billed",
    })
    assert response.status_code == 302
    response = client.post(f"/parties/{party_id}", data={
        "csrf_token": "party-test-csrf",
        "entry_date": date.today().isoformat(),
        "entry_type": "Credit",
        "amount": "5000.00",
        "reference": "RCPT-001",
        "description": "Part payment",
    })
    assert response.status_code == 302

    response = client.get(f"/parties/{party_id}")
    assert response.status_code == 200, response.status_code
    assert b"INV-001" in response.data and b"RCPT-001" in response.data

    conn = get_db()
    entries = conn.execute(
        "SELECT COUNT(*) AS count, SUM(amount) AS total FROM party_ledger_entries WHERE party_id = ?",
        (party_id,),
    ).fetchone()
    assert entries["count"] == 2
    assert round(entries["total"], 2) == 17500.50
    conn.close()

    # Invalid amounts must not create ledger rows.
    client.post(f"/parties/{party_id}", data={
        "csrf_token": "party-test-csrf",
        "entry_date": date.today().isoformat(),
        "entry_type": "Debit",
        "amount": "-10",
        "reference": "INVALID",
        "description": "Must not persist",
    })
    conn = get_db()
    assert conn.execute(
        "SELECT COUNT(*) FROM party_ledger_entries WHERE party_id = ?",
        (party_id,),
    ).fetchone()[0] == 2
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
        f"Party ledger workflow failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert database_path.exists()
