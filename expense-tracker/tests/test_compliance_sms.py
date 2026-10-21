import json

import pytest

from controller.compliance_sms import _normalise_mobile, _render_message, _send_my_sms_gateway


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


def test_my_sms_gateway_sends_documented_payload(monkeypatch):
    import controller.compliance_sms as sms

    monkeypatch.setenv("MY_SMS_GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("MY_SMS_GATEWAY_DEVICE_ID", "device-123")
    captured = {}

    class FakeResponse:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"success":true,"status":"queued","job_id":"job-1"}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = req.headers
        captured["payload"] = json.loads(req.data.decode())
        return FakeResponse()

    monkeypatch.setattr(sms, "urlopen", fake_urlopen)
    request_id, response_message = _send_my_sms_gateway("9876543210", "FleetFlow test")
    assert captured["url"] == "https://api.mysmsgateway.com/v1/sms/send"
    assert captured["payload"] == {
        "device_id": "device-123",
        "to": "+919876543210",
        "message": "FleetFlow test",
        "sim": 1,
        "message_type": "transactional",
    }
    assert request_id == "job-1"
    assert response_message == "queued"


def test_my_sms_gateway_requires_key_and_device(monkeypatch):
    monkeypatch.delenv("MY_SMS_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("MY_SMS_GATEWAY_DEVICE_ID", raising=False)
    with pytest.raises(RuntimeError, match="MY_SMS_GATEWAY_API_KEY"):
        _send_my_sms_gateway("9876543210", "test")
