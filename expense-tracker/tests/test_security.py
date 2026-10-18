"""Security smoke tests isolated from the developer's working database."""
import os
import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_authentication_security_and_landing_page(tmp_path):
    database_path = tmp_path / "test-tms.db"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_PATH": str(database_path),
            "FLASK_SECRET_KEY": "test-only-secret-key-that-is-at-least-32-characters-long",
            "APP_ENV": "test",
            "PYTHONPATH": str(PROJECT_DIR),
        }
    )
    code = r"""
from app import app, _password_matches
from werkzeug.security import generate_password_hash

assert _password_matches(generate_password_hash("Correct-Horse-123!"), "Correct-Horse-123!")
assert not _password_matches(generate_password_hash("Correct-Horse-123!"), "wrong-password")
assert not _password_matches("password123", "password123")
assert app.config["SESSION_COOKIE_HTTPONLY"] is True
assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
assert app.debug is False
with app.test_client() as client:
    response = client.get("/")
    assert response.status_code == 200, response.status_code
    # Sign-out must not mutate session state through a cross-site GET or invalid CSRF token.
    assert client.get("/logout").status_code == 405
    with client.session_transaction() as session:
        session["user_id"] = 999
        session["csrf_token"] = "expected-token"
    response = client.post("/logout", data={"csrf_token": "wrong-token"})
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session.get("user_id") == 999
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"Security smoke test failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert database_path.exists()



def test_password_reset_generates_fallback_link_when_email_is_unavailable(tmp_path):
    """In development, users can use the generated one-time link when SMTP is unavailable."""
    database_path = tmp_path / "reset-test-tms.db"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_PATH": str(database_path),
            "FLASK_SECRET_KEY": "test-only-secret-key-that-is-at-least-32-characters-long",
            "APP_ENV": "test",
            "PYTHONPATH": str(PROJECT_DIR),
        }
    )
    code = r"""
import re
import app as app_module
from app import app
from database.db import get_db
from werkzeug.security import generate_password_hash

app_module.NotificationService.send_password_reset_email = staticmethod(
    lambda recipient, reset_url: False
)
conn = get_db()
conn.execute(
    "INSERT INTO users (email, password, full_name, is_approved, is_rejected, "
    "is_terminated, role, job_role) VALUES (?, ?, ?, 1, 0, 0, 'User', 'User')",
    ("driver@example.com", generate_password_hash("Original-password-123!"), "Test Driver")
)
conn.commit()
conn.close()

with app.test_client() as client:
    page = client.get("/forgot-password")
    assert page.status_code == 200
    with client.session_transaction() as session:
        csrf_token = session["csrf_token"]
    response = client.post(
        "/forgot-password",
        data={"email": "driver@example.com", "csrf_token": csrf_token},
    )
    assert response.status_code == 200
    assert b"Your password reset link (development mode)" in response.data
    match = re.search(rb'value="http://localhost/reset-password/([^"]+)"', response.data)
    assert match, response.data.decode("utf-8", errors="replace")
    reset_token = match.group(1).decode("ascii")
    reset_page = client.get("/reset-password/" + reset_token)
    assert reset_page.status_code == 200, reset_page.status_code
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        "Password reset fallback test failed.\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


def test_only_super_admin_can_approve_admin_access(tmp_path):
    database_path = tmp_path / "admin-approval-test-tms.db"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_PATH": str(database_path),
            "FLASK_SECRET_KEY": "test-only-secret-key-that-is-at-least-32-characters-long",
            "FLEETFLOW_SUPER_ADMIN_EMAIL": "super-admin@example.test",
            "APP_ENV": "test",
            "PYTHONPATH": str(PROJECT_DIR),
        }
    )
    code = r"""
import app as app_module
from app import app
from database.db import get_db
from werkzeug.security import generate_password_hash

app_module.NotificationService.send_email = staticmethod(lambda **kwargs: None)
conn = get_db()
password = generate_password_hash("Correct-Horse-123!")
super_admin_id = conn.execute(
    "INSERT INTO users (email, password, full_name, is_approved, role, job_role) "
    "VALUES (?, ?, ?, 1, 'Admin', 'Admin')",
    ("super-admin@example.test", password, "Super Admin"),
).lastrowid
admin_id = conn.execute(
    "INSERT INTO users (email, password, full_name, is_approved, role, job_role) "
    "VALUES (?, ?, ?, 1, 'Admin', 'Admin')",
    ("admin@example.test", password, "Admin"),
).lastrowid
pending_id = conn.execute(
    "INSERT INTO users (email, password, full_name, is_approved, requested_role) "
    "VALUES (?, ?, ?, 0, 'Admin')",
    ("pending-admin@example.test", password, "Pending Admin"),
).lastrowid
conn.commit()
conn.close()

with app.test_client() as client:
    with client.session_transaction() as session:
        session.update(
            user_id=super_admin_id,
            email="super-admin@example.test",
            csrf_token="test-token",
        )
    response = client.get("/admin/users")
    assert response.status_code == 200
    assert b'<button type="submit" class="btn-primary">Approve Admin Access</button>' in response.data

with app.test_client() as client:
    with client.session_transaction() as session:
        session.update(
            user_id=admin_id,
            email="admin@example.test",
            csrf_token="test-token",
        )
    response = client.get("/admin/users")
    assert response.status_code == 200
    assert b"Awaiting super administrator approval." in response.data
    assert b'<button type="submit" class="btn-primary">Approve Admin Access</button>' not in response.data
    assert b'name="approved_role" value="Admin"' not in response.data
    response = client.post(
        f"/admin/approve/{pending_id}",
        data={"csrf_token": "test-token", "approved_role": "Admin"},
    )
    assert response.status_code == 302

conn = get_db()
pending = conn.execute(
    "SELECT is_approved, role FROM users WHERE user_id = ?", (pending_id,)
).fetchone()
assert pending["is_approved"] == 0
conn.close()

with app.test_client() as client:
    with client.session_transaction() as session:
        session.update(
            user_id=super_admin_id,
            email="super-admin@example.test",
            csrf_token="test-token",
        )
    response = client.post(
        f"/admin/approve/{pending_id}",
        data={"csrf_token": "test-token", "approved_role": "Admin"},
    )
    assert response.status_code == 302

conn = get_db()
pending = conn.execute(
    "SELECT is_approved, role FROM users WHERE user_id = ?", (pending_id,)
).fetchone()
assert pending["is_approved"] == 1
assert pending["role"] == "Admin"
conn.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        "Administrator access approval test failed.\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


def test_admin_user_driver_selector_explains_empty_state_and_lists_available_drivers(tmp_path):
    database_path = tmp_path / "admin-driver-selector-test-tms.db"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_PATH": str(database_path),
            "FLASK_SECRET_KEY": "test-only-secret-key-that-is-at-least-32-characters-long",
            "FLEETFLOW_SUPER_ADMIN_EMAIL": "super-admin@example.test",
            "APP_ENV": "test",
            "PYTHONPATH": str(PROJECT_DIR),
        }
    )
    code = r"""
from app import app
from database.db import get_db

conn = get_db()
admin_id = conn.execute(
    "INSERT INTO users (email, password, full_name, is_approved, role, job_role) "
    "VALUES (?, ?, ?, 1, 'Admin', 'Admin')",
    ("super-admin@example.test", "unused-test-password", "Super Admin"),
).lastrowid
conn.commit()
conn.close()

with app.test_client() as client:
    with client.session_transaction() as session:
        session.update(
            user_id=admin_id,
            email="super-admin@example.test",
            csrf_token="test-token",
        )
    response = client.get("/admin/users")
    assert response.status_code == 200
    assert b'id="new-driver-record"' in response.data
    assert b"No active, unlinked driver records" in response.data
    assert b'href="/drivers/add">Add a driver record</a>' in response.data

conn = get_db()
conn.execute(
    "INSERT INTO drivers (name, license_number, status) VALUES (?, ?, 'Active')",
    ("Available Driver", "TEST-LIC-001"),
)
conn.commit()
conn.close()

with app.test_client() as client:
    with client.session_transaction() as session:
        session.update(
            user_id=admin_id,
            email="super-admin@example.test",
            csrf_token="test-token",
        )
    response = client.get("/admin/users")
    assert response.status_code == 200
    assert b'<option value="1">Available Driver \xe2\x80\x94 TEST-LIC-001</option>' in response.data
    assert b'<select id="new-driver-record" name="driver_id" class="form-input" required disabled>' not in response.data
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        "Driver record selector test failed.\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
