import sys
from pathlib import Path

# The project root for this application is expense-tracker/, not the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask, session

from controller.ai_assistant import (
    _detect_intent, _format_answer_for_user, _parse_payment_prompt,
    _parse_trip_prompt, _resolve_contextual_question, _validate_graph_answer, assistant_graph,
)


def test_intent_router_recognizes_trip_questions():
    assert _detect_intent("Show my latest trips") == "trips"


def test_intent_router_recognizes_pending_trip_count_in_hinglish():
    assert _detect_intent("Abhi kitne trips pending hain?") == "trips"


def test_vague_details_follow_up_uses_single_trip_from_previous_result():
    history = [
        {"role": "user", "content": "Abhi kitne trips pending hain?"},
        {"role": "assistant", "content": "\n".join(["| Trip ID | Route | Date | Status |", "| --- | --- | --- | --- |", "| #7 | Bhopal → Indore | 2026-10-21 | Scheduled |", "Abhi 1 trip pending hai."])},
    ]
    assert _resolve_contextual_question("give me all details", history) == "Trip #7 details"
    assert _resolve_contextual_question("give me complete details", history) == "Trip #7 details"
    assert _resolve_contextual_question("show complete details", history) == "Trip #7 details"


def test_intent_router_recognizes_expense_questions():
    assert _detect_intent("How much did we spend on expenses?") == "expenses"


def test_intent_router_recognizes_payments_and_maintenance():
    assert _detect_intent("How much payment has been received?") == "payments"
    assert _detect_intent("Show maintenance service costs") == "maintenance"


def test_intent_router_recognizes_hinglish_and_fleet_records():
    assert _detect_intent("Trip 4 ka status batao") == "trips"
    assert _detect_intent("KA50EU1043 wali gaadi dikhao") == "trucks"
    assert _detect_intent("Rakesh driver ka record") == "drivers"
    assert _detect_intent("pending challan dikhao") == "challans"


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



def test_answer_quality_gate_replaces_empty_answers():
    app = Flask(__name__)
    with app.app_context():
        result = _validate_graph_answer({"answer": "  ", "intent": "trips", "request_id": "test"})
    assert "couldn't verify" in result["answer"].lower()


def test_saved_hinglish_preference_applies_to_english_follow_up(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    answer = _format_answer_for_user(
        "show details",
        "Trip details: #4 Fatuha to Patna",
        ["I prefer Hinglish answers"],
    )
    assert answer.startswith("Trip ki details:")


def test_trip_prompt_parser_extracts_required_details():
    app = Flask(__name__)
    app.secret_key = "test-only-secret-key"
    with app.test_request_context("/assistant"):
        session["email"] = "admin@example.com"
        draft, missing = _parse_trip_prompt(
            "Create a trip from Bhopal to Indore tomorrow using truck MP04AB1234, "
            "driver Rajesh Kumar, freight 25000."
        )
    assert missing == []
    assert draft["route_from"] == "Bhopal"
    assert draft["route_to"] == "Indore"
    assert draft["start_date"]
    assert draft["truck_label"] == "MP04AB1234"
    assert draft["driver_label"] == "Rajesh Kumar"
    assert draft["freight_amount"] == "25000.00"
    assert draft["owner_email"] == "admin@example.com"


def test_payment_intent_recognizes_received_amount_for_trip():
    assert _detect_intent("I got 10000 amount for trip3") == "payments"


def test_payment_prompt_understands_trip_number_without_space():
    draft, missing = _parse_payment_prompt("I got 10000 amount for trip3")
    assert missing == []
    assert draft["trip_id"] == 3
    assert draft["amount"] == "10000.00"

from controller.roadside_compliance import DOCUMENT_TYPES, FINDINGS, INSPECTION_TYPES, SEVERITIES, STATUSES


def test_roadside_compliance_catalog_covers_requested_inspection_areas():
    assert "Driving Licence (HMV)" in DOCUMENT_TYPES
    assert "Registration Certificate (RC)" in DOCUMENT_TYPES
    assert "Commercial Permit" in DOCUMENT_TYPES
    assert "Fitness Certificate" in DOCUMENT_TYPES
    assert "PUC Certificate" in DOCUMENT_TYPES
    assert "Third-Party Insurance" in DOCUMENT_TYPES
    assert "Trip Sheet / E-Way Bill / Cargo Manifest" in DOCUMENT_TYPES
    assert {"Documents", "Weight and Cargo", "Vehicle Safety", "Driver Behaviour", "Operational Rules"} <= INSPECTION_TYPES
    assert "Overloading / weight limit concern" in FINDINGS
    assert "RUPD missing / damaged" in FINDINGS
    assert "LUPD missing / damaged" in FINDINGS
    assert "Mobile phone use while driving" in FINDINGS
    assert "Unauthorized passengers in goods vehicle" in FINDINGS
    assert {"Low", "Medium", "High", "Critical"} == SEVERITIES
    assert {"Open", "Corrected", "Referred", "Closed"} == STATUSES


def test_intent_router_covers_common_fleet_synonyms_and_spelling_variants():
    cases = {
        "gaadi ka status batao": "trucks",
        "Meri gaadiyan dikhao": "trucks",
        "show vehicle registration number": "trucks",
        "Rakesh chalak ka record": "drivers",
        "driver ka licence number": "drivers",
        "show e challan and traffic fine": "challans",
        "pending chalan kitna hai": "challans",
        "diesel ka kharcha kitna laga": "expenses",
        "truck repair cost": "expenses",
        "trip 3 ka due amount": "payments",
        "trip 4 ke paise mile": "payments",
        "gaadi servicing kab hui": "maintenance",
        "truck breakdown at highway": "maintenance",
        "trip bnao Bhopal se Indore": "trips",
        "show completed shipments": "trips",
        "PUC certificate expiry kab hai": "help",
        "insurance expired": "help",
        "Parivahan RC status": "help",
    }
    for question, expected in cases.items():
        assert _detect_intent(question) == expected, question


def test_intent_router_does_not_confuse_spending_with_received_payments():
    assert _detect_intent("kitna paisa laga diesel mein") == "expenses"
    assert _detect_intent("trip 3 ke paise mile") == "payments"
