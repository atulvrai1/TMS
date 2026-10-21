from datetime import date

from controller.roadside_compliance import classify_expiry


def test_expiry_classification_handles_missing_date():
    assert classify_expiry(None, date(2026, 10, 21)) == "Expiry date missing"


def test_expiry_classification_distinguishes_expired_and_due_today():
    today = date(2026, 10, 21)
    assert classify_expiry("2026-10-20", today) == "Expired"
    assert classify_expiry("2026-10-21", today) == "Due today"


def test_expiry_classification_uses_reminder_windows():
    today = date(2026, 10, 21)
    assert classify_expiry("2026-10-22", today) == "Urgent"
    assert classify_expiry("2026-10-28", today) == "Urgent"
    assert classify_expiry("2026-10-29", today) == "Expiring soon"
    assert classify_expiry("2026-11-20", today) == "Expiring soon"
    assert classify_expiry("2026-11-21", today) == "Valid"


def test_expiry_classification_rejects_invalid_date():
    assert classify_expiry("not-a-date", date(2026, 10, 21)) == "Invalid expiry date"
