import sys
from pathlib import Path

# The application root is expense-tracker/, one level above this test folder.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as fleetflow_app


class FakeConnection:
    def __init__(self):
        self.closed = False

    def execute(self, statement):
        assert statement == "SELECT 1"
        return self

    def fetchone(self):
        return (1,)

    def close(self):
        self.closed = True


def test_health_endpoint_returns_ok_when_database_is_available(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(fleetflow_app, "get_db", lambda: connection)

    response = fleetflow_app.app.test_client().get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    assert connection.closed


def test_health_endpoint_returns_unhealthy_when_database_fails(monkeypatch):
    def fail_to_connect():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(fleetflow_app, "get_db", fail_to_connect)

    response = fleetflow_app.app.test_client().get("/healthz")

    assert response.status_code == 503
    assert response.get_json() == {"status": "unhealthy"}
