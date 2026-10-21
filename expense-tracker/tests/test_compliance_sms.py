import json

import pytest

from controller.compliance_sms import (
    _normalise_mobile,
    _template_parameters,
    _send_whatsapp_template,
)


def test_normalise_indian_mobile_accepts_country_code_and_formatting():
    assert _normalise_mobile("+91 98765-43210") == "9876543210"


@pytest.mark.parametrize("value", ["", "1234567890", "5123456789", "987654321", "9198765432100"])
def test_normalise_mobile_rejects_invalid_numbers(value):
    with pytest.raises(ValueError):
        _normalise_mobile(value)


def test_template_parameters_contain_document_asset_and_expiry():
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
    params = _template_parameters(row, 7)
    assert params == [
        "Third-Party Insurance",
        "BR-XX-1234",
        "Expires in 7 days on 2026-10-27",
    ]


def test_whatsapp_cloud_api_sends_approved_template(monkeypatch):
    import controller.compliance_sms as whatsapp

    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456789")
    monkeypatch.setenv("WHATSAPP_API_VERSION", "v23.0")
    monkeypatch.setenv("WHATSAPP_TEMPLATE_NAME", "fleetflow_compliance_alert")
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"messaging_product":"whatsapp","messages":[{"id":"wamid.test-1"}]}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = req.headers
        captured["payload"] = json.loads(req.data.decode())
        return FakeResponse()

    monkeypatch.setattr(whatsapp, "urlopen", fake_urlopen)
    request_id, response_message = _send_whatsapp_template(
        "9876543210", ["Insurance", "BR-XX-1234", "Expires in 7 days"]
    )

    assert captured["url"] == "https://graph.facebook.com/v23.0/123456789/messages"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["payload"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "919876543210",
        "type": "template",
        "template": {
            "name": "fleetflow_compliance_alert",
            "language": {"code": "en"},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": "Insurance"},
                    {"type": "text", "text": "BR-XX-1234"},
                    {"type": "text", "text": "Expires in 7 days"},
                ],
            }],
        },
    }
    assert request_id == "wamid.test-1"
    assert "accepted" in response_message.lower()


def test_whatsapp_cloud_api_requires_credentials(monkeypatch):
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    with pytest.raises(RuntimeError, match="WHATSAPP_ACCESS_TOKEN"):
        _send_whatsapp_template("9876543210", ["Insurance", "Truck 1", "Expires in 7 days"])
