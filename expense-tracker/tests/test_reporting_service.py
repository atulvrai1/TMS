import sqlite3
import unittest
from datetime import date

from controller.reporting_service import _date_range, build_report


class ReportingServiceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE trips (
                trip_id INTEGER PRIMARY KEY, owner_user_id INTEGER, driver_id INTEGER,
                truck_id INTEGER, start_date TEXT, end_date TEXT, status TEXT, freight_amount REAL
            );
            CREATE TABLE trucks (truck_id INTEGER PRIMARY KEY, registration_number TEXT);
            CREATE TABLE trip_payments (
                payment_id INTEGER PRIMARY KEY, trip_id INTEGER, amount REAL, payment_date TEXT
            );
            CREATE TABLE expenses (
                expense_id INTEGER PRIMARY KEY, trip_id INTEGER, amount REAL, status TEXT, date TEXT
            );
            CREATE TABLE maintenance (
                maintenance_id INTEGER PRIMARY KEY, owner_user_id INTEGER, truck_id INTEGER,
                cost REAL, service_date TEXT
            );
        """)
        today = date.today().isoformat()
        self.conn.execute("INSERT INTO trucks VALUES (1, 'BR-01-AA-1234')")
        self.conn.execute(
            "INSERT INTO trips VALUES (1, 7, 70, 1, ?, ?, 'Completed', 100000)",
            (today, today),
        )
        self.conn.execute("INSERT INTO trip_payments VALUES (1, 1, 60000, ?)", (today,))
        self.conn.execute("INSERT INTO expenses VALUES (1, 1, 12000, 'Approved', ?)", (today,))
        self.conn.execute("INSERT INTO expenses VALUES (2, 1, 9000, 'Pending', ?)", (today,))
        self.conn.execute("INSERT INTO maintenance VALUES (1, 7, 1, 3000, ?)", (today,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_all_time_date_range(self):
        start, end, label = _date_range("show overall report")
        self.assertEqual(start, "1900-01-01")
        self.assertEqual(end, date.today().isoformat())
        self.assertEqual(label, "all time")

    def test_report_uses_approved_expenses_and_separates_collections(self):
        def scoped(alias, owner_column, state):
            if alias in ("t", "m"):
                return f"{alias}.{owner_column} = ?", [7]
            return f"{alias}.{owner_column} = ?", [7]

        result = build_report(self.conn, {"user_id": 7}, "show overall report", scoped)
        answer = result["answer"]
        self.assertIn("₹100,000.00", answer)
        self.assertIn("₹60,000.00", answer)
        self.assertIn("₹12,000.00", answer)
        self.assertIn("₹3,000.00", answer)
        self.assertIn("₹85,000.00", answer)
        self.assertNotIn("₹9,000.00", answer)

    def test_chart_request_returns_actual_query_values(self):
        def scoped(alias, owner_column, state):
            return "1 = 1", []

        result = build_report(self.conn, {"user_id": 7}, "show monthly revenue chart", scoped)
        self.assertIsNotNone(result["chart"])
        self.assertEqual(result["chart"]["type"], "line")
        self.assertTrue(result["chart"]["labels"])
        self.assertEqual(result["chart"]["datasets"][0]["data"][-1], 100000.0)


if __name__ == "__main__":
    unittest.main()
