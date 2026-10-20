from datetime import date

import pytest

from controller.compliance_sms import _msg07_payload, _normalise_mobile, _render_message


def test_normalise_indian_mobile_accepts_country_code_and_formatting():
    assert _normalise_mobile("+91 98765-43210") == "9876543210"


@pytest.mark.parametrize("value", ["", "1234567890", "5123456789", "987654321", "9198765432100"])
def test_normalise_mobile_rejects_invalid_numbers(value):
    with pytest.raises(ValueError):
        _normalise_mobile(value)


def test_render_message_contains_document_asset_and_expiry():
    row = {
        "document_type": "Third-Party Insurance",
        "subject_type": "truck",
        "registration_number": "BR-XX-1234",
        "truck_id": 1,
        "driver_id": None,
        "trip_id": None,
        "driver_name": None,
        "expiry_date": "2026-10-27",
    }
    message = _render_message(row, 7)
    assert "Third-Party Insurance" in message
    assert "BR-XX-1234" in message
    assert "7 days" in message
    assert "2026-10-27" in message



def test_msg07_payload_defaults_to_mobile_and_message(monkeypatch):
    monkeypatch.delenv("MSG07_PAYLOAD_TEMPLATE", raising=False)
    assert _msg07_payload("9876543210", "FleetFlow test") == {
        "to": "9876543210",
        "message": "FleetFlow test",
    }


def test_msg07_payload_supports_provider_specific_schema(monkeypatch):
    monkeypatch.setenv(
        "MSG07_PAYLOAD_TEMPLATE",
        '{"recipient":"{{mobile}}","text":{"body":"{{message}}"},"channel":"sms"}',
    )
    assert _msg07_payload("9876543210", "Expiry alert") == {
        "recipient": "9876543210",
        "text": {"body": "Expiry alert"},
        "channel": "sms",
    }


def test_msg07_payload_rejects_invalid_json(monkeypatch):
    monkeypatch.setenv("MSG07_PAYLOAD_TEMPLATE", "{not-json")
    with pytest.raises(RuntimeError, match="valid JSON"):
        _msg07_payload("9876543210", "test")
