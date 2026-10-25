import sqlite3

import pytest
from flask import Flask, jsonify, session

import database.db as database
from controller.access_controller import SUPER_ADMIN_EMAIL, UserAccessController, admin_required


@pytest.fixture
def access_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            email TEXT NOT NULL,
            role TEXT NOT NULL,
            is_approved INTEGER NOT NULL DEFAULT 1,
            is_rejected INTEGER NOT NULL DEFAULT 0,
            is_terminated INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.executemany(
        "INSERT INTO users (user_id, email, role) VALUES (?, ?, ?)",
        [
            (1, "driver@example.com", "User"),
            (2, "admin@example.com", "Admin"),
        ],
    )
    conn.commit()
    monkeypatch.setattr(database, "get_db", lambda: conn)
    yield conn
    conn.close()


def make_app():
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test-secret-key-for-access-tests")

    @app.get("/admin")
    @admin_required
    def admin_page():
        return jsonify(ok=True)

    @app.get("/login")
    def login():
        return "login"

    return app


def test_super_admin_email_comparison_ignores_case_and_outer_spaces():
    controller = UserAccessController(sqlite3.connect(":memory:"))
    assert controller.is_super_admin(f" {SUPER_ADMIN_EMAIL.upper()} ")
    assert not controller.is_super_admin("someone-else@example.com")


def test_admin_route_rejects_user_even_with_stale_admin_session(access_db):
    app = make_app()
    client = app.test_client()

    with client.session_transaction() as current_session:
        current_session["user_id"] = 1
        current_session["is_admin"] = True
        current_session["role"] = "Admin"
        current_session["email"] = "admin@example.com"

    response = client.get("/admin")
    assert response.status_code == 403


def test_admin_route_allows_database_admin(access_db):
    app = make_app()
    client = app.test_client()

    with client.session_transaction() as current_session:
        current_session["user_id"] = 2
        current_session["is_admin"] = False
        current_session["role"] = "User"

    response = client.get("/admin")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_admin_route_rejects_unapproved_user(access_db):
    access_db.execute("UPDATE users SET is_approved = 0 WHERE user_id = 2")
    access_db.commit()
    app = make_app()
    client = app.test_client()

    with client.session_transaction() as current_session:
        current_session["user_id"] = 2

    response = client.get("/admin")
    assert response.status_code == 302
