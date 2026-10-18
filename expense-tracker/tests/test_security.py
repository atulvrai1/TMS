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
