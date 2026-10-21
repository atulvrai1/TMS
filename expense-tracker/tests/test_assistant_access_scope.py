import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import controller.ai_assistant as assistant


class FakeCursor:
    def fetchall(self):
        return []


class FakeConnection:
    def __init__(self):
        self.calls = []

    def execute(self, query, params=()):
        self.calls.append((query, list(params)))
        return FakeCursor()

    def close(self):
        pass


def test_user_truck_query_is_scoped_to_the_signed_in_owner(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(assistant, "get_db", lambda: connection)

    result = assistant._query_records({
        "intent": "trucks",
        "question": "show trucks",
        "routing_question": "show trucks",
        "user_id": 41,
        "email": "user@example.com",
        "role": "User",
        "driver_id": None,
    })

    query, params = connection.calls[0]
    assert "WHERE trucks.owner_user_id = ?" in query
    assert params == [41]
    assert "truck" in result["answer"].lower()


def test_admin_driver_query_uses_the_admin_owner_scope(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(assistant, "get_db", lambda: connection)

    assistant._query_records({
        "intent": "drivers",
        "question": "show drivers",
        "routing_question": "show drivers",
        "user_id": 12,
        "email": "admin@example.com",
        "role": "Admin",
        "driver_id": None,
    })

    query, params = connection.calls[0]
    assert "drivers.owner_user_id IN (SELECT user_id FROM users WHERE role = 'User')" in query
    assert "drivers.owner_user_id = ?" in query
    assert params == [12]
