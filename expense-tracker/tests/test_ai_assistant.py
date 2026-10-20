from controller.ai_assistant import _detect_intent, assistant_graph


def test_intent_router_recognizes_trip_questions():
    assert _detect_intent("Show my latest trips") == "trips"


def test_intent_router_recognizes_expense_questions():
    assert _detect_intent("How much did we spend on expenses?") == "expenses"


def test_intent_router_recognizes_payments_and_maintenance():
    assert _detect_intent("How much payment has been received?") == "payments"
    assert _detect_intent("Show maintenance service costs") == "maintenance"


def test_graph_returns_safe_help_for_unsupported_questions(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    result = assistant_graph.invoke({
        "question": "Can you change my password?",
        "user_id": 123,
        "email": "driver@example.com",
        "role": "User",
        "driver_id": None,
    })
    assert result["intent"] == "help"
    assert "read-only" in result["answer"].lower()
