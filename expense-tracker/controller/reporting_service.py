"""Permission-scoped, parameterized reporting helpers for FleetFlow Assistant.

Uses the same business definitions as the Reports/P&L page:
- freight revenue is from completed trips, dated by valid completion date with
  start-date fallback;
- collections use recorded payment dates;
- expenses include Approved records only;
- maintenance is reported separately;
- operating profit is explicitly an estimate of revenue less included costs.
"""
from datetime import date, timedelta
import re


def _date_range(question):
    """Resolve common natural-language date windows; default to current month."""
    today = date.today()
    text = (question or "").lower()
    custom = re.search(r"\b(20\d{2}-\d{2}-\d{2})\s*(?:to|through|until|-)\s*(20\d{2}-\d{2}-\d{2})\b", text)
    if custom:
        try:
            start, end = date.fromisoformat(custom.group(1)), date.fromisoformat(custom.group(2))
            if start <= end:
                return start.isoformat(), end.isoformat(), "custom date range"
        except ValueError:
            pass
    if "all time" in text or "overall" in text or "ever" in text or "lifetime" in text:
        return "1900-01-01", today.isoformat(), "all time"
    if "last year" in text or "previous year" in text:
        return date(today.year - 1, 1, 1).isoformat(), date(today.year - 1, 12, 31).isoformat(), "last calendar year"
    if "this year" in text or "this financial year" in text or "current financial year" in text:
        start_year = today.year if today.month >= 4 else today.year - 1
        return date(start_year, 4, 1).isoformat(), today.isoformat(), "current financial year" if "financial" in text else "this year"
    if "last month" in text or "previous month" in text:
        first = today.replace(day=1)
        end = first - timedelta(days=1)
        return end.replace(day=1).isoformat(), end.isoformat(), "last month"
    if "this week" in text or "this week" in text:
        start = today - timedelta(days=today.weekday())
        return start.isoformat(), today.isoformat(), "this week"
    if "today" in text:
        return today.isoformat(), today.isoformat(), "today"
    if "yesterday" in text:
        d = today - timedelta(days=1)
        return d.isoformat(), d.isoformat(), "yesterday"
    if "last 12 months" in text or "past 12 months" in text or "yearly trend" in text:
        start = date(today.year - 1, today.month, 1)
        return start.isoformat(), today.isoformat(), "last 12 months"
    if re.search(r"\b(chart|graph|trend|monthly|month wise|month-wise|over time)\b", text):
        start = date(today.year - 1, today.month, 1)
        return start.isoformat(), today.isoformat(), "last 12 months"
    return today.replace(day=1).isoformat(), today.isoformat(), "this month"


def build_report(conn, state, question, owner_scope):
    """Return a factual answer and optional chart payload, scoped to user access."""
    start, end, period_label = _date_range(question)
    trip_scope, trip_params = owner_scope("t", "owner_user_id", state)
    expense_scope, expense_params = owner_scope("t", "owner_user_id", state)
    payment_scope, payment_params = owner_scope("t", "owner_user_id", state)
    maintenance_scope, maintenance_params = owner_scope("m", "owner_user_id", state)
    completion_date = "CASE WHEN t.end_date IS NOT NULL AND t.end_date >= t.start_date THEN t.end_date ELSE t.start_date END"

    revenue = conn.execute(
        "SELECT COALESCE(SUM(t.freight_amount),0) AS amount, COUNT(*) AS count "
        f"FROM trips t WHERE {trip_scope} AND t.status='Completed' AND {completion_date} BETWEEN ? AND ?",
        [*trip_params, start, end],
    ).fetchone()
    collections = conn.execute(
        "SELECT COALESCE(SUM(p.amount),0) AS amount, COUNT(*) AS count "
        "FROM trip_payments p JOIN trips t ON t.trip_id=p.trip_id "
        f"WHERE {payment_scope} AND p.payment_date BETWEEN ? AND ?",
        [*payment_params, start, end],
    ).fetchone()
    expenses = conn.execute(
        "SELECT COALESCE(SUM(e.amount),0) AS amount, COUNT(*) AS count "
        "FROM expenses e JOIN trips t ON t.trip_id=e.trip_id "
        f"WHERE {expense_scope} AND e.status='Approved' AND e.date BETWEEN ? AND ?",
        [*expense_params, start, end],
    ).fetchone()
    maintenance = conn.execute(
        "SELECT COALESCE(SUM(m.cost),0) AS amount, COUNT(*) AS count "
        f"FROM maintenance m WHERE {maintenance_scope} AND m.service_date BETWEEN ? AND ?",
        [*maintenance_params, start, end],
    ).fetchone()
    trips = conn.execute(
        "SELECT COUNT(*) AS total, "
        "COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0) AS completed, "
        "COALESCE(SUM(CASE WHEN status IN ('Scheduled','In Progress') THEN 1 ELSE 0 END),0) AS active, "
        "COALESCE(SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END),0) AS cancelled "
        f"FROM trips t WHERE {trip_scope} AND t.start_date BETWEEN ? AND ?",
        [*trip_params, start, end],
    ).fetchone()

    # Use a bounded, fixed SQL shape; never accept SQL, column names or sort keys
    # from the model or user.
    monthly = conn.execute(
        "SELECT strftime('%Y-%m', " + completion_date + ") AS label, "
        "COALESCE(SUM(t.freight_amount),0) AS revenue "
        f"FROM trips t WHERE {trip_scope} AND t.status='Completed' "
        f"AND {completion_date} BETWEEN ? AND ? GROUP BY label ORDER BY label",
        [*trip_params, start, end],
    ).fetchall()
    top_trucks = conn.execute(
        "SELECT COALESCE(tr.registration_number,'Not assigned') AS label, "
        "COALESCE(SUM(t.freight_amount),0) AS revenue "
        "FROM trips t LEFT JOIN trucks tr ON tr.truck_id=t.truck_id "
        f"WHERE {trip_scope} AND t.status='Completed' AND {completion_date} BETWEEN ? AND ? "
        "GROUP BY label ORDER BY revenue DESC LIMIT 8",
        [*trip_params, start, end],
    ).fetchall()

    fmt = lambda value: "₹{:,.2f}".format(float(value or 0))
    answer = (
        f"**FleetFlow report — {period_label} ({start} to {end})**\n\n"
        f"- Completed-trip freight: **{fmt(revenue['amount'])}** ({int(revenue['count'] or 0)} trips)\n"
        f"- Payments recorded: **{fmt(collections['amount'])}** ({int(collections['count'] or 0)} payments)\n"
        f"- Approved expenses: **{fmt(expenses['amount'])}** ({int(expenses['count'] or 0)} records)\n"
        f"- Maintenance costs: **{fmt(maintenance['amount'])}** ({int(maintenance['count'] or 0)} records)\n"
        f"- Estimated operating profit: **{fmt(float(revenue['amount'] or 0) - float(expenses['amount'] or 0) - float(maintenance['amount'] or 0))}**\n"
        f"- Trips started in period: **{int(trips['total'] or 0)}** (completed {int(trips['completed'] or 0)}, active {int(trips['active'] or 0)}, cancelled {int(trips['cancelled'] or 0)})\n\n"
        "Profit is an estimate: it subtracts approved expenses and recorded maintenance from completed-trip freight. "
        "It does not include costs absent from these records. Freight earned and cash collected are different measures."
    )
    text = (question or "").lower()
    chart = None
    if re.search(r"\b(chart|graph|trend|over time|monthly|month wise|month-wise|last 12 months)\b", text):
        chart = {
            "type": "line",
            "title": "Completed-trip freight by month",
            "labels": [row["label"] or "Unknown" for row in monthly],
            "datasets": [{"label": "Freight (₹)", "data": [round(float(row["revenue"] or 0), 2) for row in monthly]}],
        }
    elif re.search(r"\b(truck|vehicle|top|highest|best performing)\b", text):
        chart = {
            "type": "bar",
            "title": "Freight by truck",
            "labels": [row["label"] for row in top_trucks],
            "datasets": [{"label": "Freight (₹)", "data": [round(float(row["revenue"] or 0), 2) for row in top_trucks]}],
        }
    return {"answer": answer, "chart": chart, "report_period": {"start": start, "end": end}}
