"""Traffic challan tracking, payment status and due-date alerts for FleetFlow."""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import abort, flash, redirect, render_template, request, session, url_for

CHALLAN_STATUSES = {"Pending", "Paid", "Disputed"}


def _ensure_challan_schema(get_db):
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS challans (
                challan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                truck_id INTEGER NOT NULL,
                challan_number TEXT NOT NULL,
                violation TEXT NOT NULL,
                issue_date DATE NOT NULL,
                due_date DATE,
                amount REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'Pending',
                issuing_authority TEXT,
                location TEXT,
                payment_date DATE,
                notes TEXT,
                owner_user_id INTEGER REFERENCES users(user_id),
                created_by INTEGER REFERENCES users(user_id),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME,
                FOREIGN KEY (truck_id) REFERENCES trucks(truck_id)
            )
        """)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(challans)")}
        additions = {
            "truck_id": "INTEGER",
            "challan_number": "TEXT",
            "violation": "TEXT",
            "issue_date": "DATE",
            "due_date": "DATE",
            "amount": "REAL NOT NULL DEFAULT 0",
            "status": "TEXT NOT NULL DEFAULT 'Pending'",
            "issuing_authority": "TEXT",
            "location": "TEXT",
            "payment_date": "DATE",
            "notes": "TEXT",
            "owner_user_id": "INTEGER REFERENCES users(user_id)",
            "created_by": "INTEGER REFERENCES users(user_id)",
            "created_at": "DATETIME",
            "updated_at": "DATETIME",
        }
        for name, definition in additions.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE challans ADD COLUMN {name} {definition}")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_challans_owner_number "
            "ON challans(owner_user_id, challan_number) "
            "WHERE challan_number IS NOT NULL AND challan_number != ''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_challans_due_status "
            "ON challans(status, due_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_challans_truck "
            "ON challans(truck_id, issue_date DESC)"
        )


def register_challan_module(app, ns):
    get_db = ns["get_db"]
    admin_required = ns["admin_required"]
    _record_owner_filter = ns["_record_owner_filter"]
    _valid_csrf_token = ns["_valid_csrf_token"]
    _record_user_activity = ns["_record_user_activity"]
    _ensure_challan_schema(get_db)

    def scoped_challan(conn, challan_id):
        scope, params = _record_owner_filter("challans")
        return conn.execute(
            "SELECT challans.* FROM challans "
            f"WHERE challans.challan_id = ? AND ({scope})",
            [challan_id, *params],
        ).fetchone()

    def parse_form():
        truck_id = request.form.get("truck_id", "").strip()
        number = request.form.get("challan_number", "").strip()
        violation = request.form.get("violation", "").strip()
        issue_date = request.form.get("issue_date", "").strip()
        due_date = request.form.get("due_date", "").strip() or None
        payment_date = request.form.get("payment_date", "").strip() or None
        status = request.form.get("status", "Pending").strip()
        authority = request.form.get("issuing_authority", "").strip()
        location = request.form.get("location", "").strip()
        notes = request.form.get("notes", "").strip()

        if not truck_id.isdigit():
            raise ValueError("Select a valid truck.")
        if not number or len(number) > 80:
            raise ValueError("Challan number is required and must be 80 characters or fewer.")
        if not violation or len(violation) > 160:
            raise ValueError("Enter the violation/reason (up to 160 characters).")
        if status not in CHALLAN_STATUSES:
            raise ValueError("Choose a valid challan status.")
        for value, label in ((issue_date, "Issue date"), (due_date, "Due date"), (payment_date, "Payment date")):
            if value:
                try:
                    if date.fromisoformat(value).isoformat() != value:
                        raise ValueError
                except ValueError:
                    raise ValueError(f"{label} must be a valid date.")
        if not issue_date:
            raise ValueError("Issue date is required.")
        if due_date and due_date < issue_date:
            raise ValueError("Due date cannot be earlier than the issue date.")
        if payment_date and payment_date < issue_date:
            raise ValueError("Payment date cannot be earlier than the issue date.")
        if status == "Paid" and not payment_date:
            payment_date = date.today().isoformat()
        if status != "Paid":
            payment_date = None
        if len(authority) > 120 or len(location) > 160 or len(notes) > 500:
            raise ValueError("Authority, location, or notes exceed the allowed length.")
        try:
            amount = Decimal((request.form.get("amount") or "0").strip()).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        except (InvalidOperation, AttributeError):
            raise ValueError("Fine amount must be a valid number.")
        if not amount.is_finite() or amount < 0 or amount > Decimal("999999999.99"):
            raise ValueError("Fine amount must be between 0 and 999,999,999.99.")
        return {
            "truck_id": int(truck_id),
            "challan_number": number,
            "violation": violation,
            "issue_date": issue_date,
            "due_date": due_date,
            "amount": float(amount),
            "status": status,
            "issuing_authority": authority or None,
            "location": location or None,
            "payment_date": payment_date,
            "notes": notes or None,
        }

    @app.route("/challans", methods=["GET"], endpoint="challans")
    @admin_required
    def challans():
        session.setdefault("csrf_token", __import__("secrets").token_urlsafe(32))
        conn = get_db()
        scope, scope_params = _record_owner_filter("challans")
        status_filter = request.args.get("status", "").strip()
        if status_filter not in CHALLAN_STATUSES:
            status_filter = ""
        query = request.args.get("q", "").strip()[:100]
        clauses = [scope]
        params = list(scope_params)
        if status_filter:
            clauses.append("challans.status = ?")
            params.append(status_filter)
        if query:
            clauses.append(
                "(challans.challan_number LIKE ? OR challans.violation LIKE ? "
                "OR trucks.registration_number LIKE ? OR COALESCE(challans.issuing_authority, '') LIKE ?)"
            )
            pattern = f"%{query}%"
            params.extend([pattern, pattern, pattern, pattern])
        rows = conn.execute(
            "SELECT challans.*, trucks.registration_number, trucks.model "
            "FROM challans JOIN trucks ON trucks.truck_id = challans.truck_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY CASE WHEN challans.status = 'Paid' THEN 2 "
            "WHEN challans.due_date < date('now') THEN 0 ELSE 1 END, "
            "challans.due_date ASC, challans.issue_date DESC",
            params,
        ).fetchall()
        trucks = conn.execute(
            "SELECT truck_id, registration_number, model FROM trucks "
            "ORDER BY registration_number COLLATE NOCASE"
        ).fetchall()
        summary = conn.execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN status != 'Paid' THEN amount ELSE 0 END), 0) AS unpaid_amount, "
            "SUM(CASE WHEN status = 'Pending' THEN 1 ELSE 0 END) AS pending, "
            "SUM(CASE WHEN status = 'Paid' THEN 1 ELSE 0 END) AS paid, "
            "SUM(CASE WHEN status = 'Disputed' THEN 1 ELSE 0 END) AS disputed "
            f"FROM challans WHERE {scope}",
            scope_params,
        ).fetchone()
        today = date.today().isoformat()
        due_soon = conn.execute(
            "SELECT COUNT(*) AS count FROM challans "
            f"WHERE {scope} AND status = 'Pending' AND due_date >= ? "
            "AND due_date <= date(?, '+30 days')",
            [*scope_params, today, today],
        ).fetchone()["count"]
        overdue = conn.execute(
            "SELECT COUNT(*) AS count FROM challans "
            f"WHERE {scope} AND status = 'Pending' AND due_date < ?",
            [*scope_params, today],
        ).fetchone()["count"]
        conn.close()
        return render_template(
            "challans.html",
            challans=rows,
            trucks=trucks,
            summary=summary,
            due_soon=due_soon,
            overdue=overdue,
            selected_status=status_filter,
            search_query=query,
            today=today,
            csrf_token=session["csrf_token"],
            block_content="dashboard",
        )

    @app.route("/challans/add", methods=["POST"], endpoint="challan_add")
    @admin_required
    def challan_add():
        if not _valid_csrf_token():
            flash("Your session token is invalid. Reload the page and try again.", "error")
            return redirect(url_for("challans"))
        conn = get_db()
        try:
            data = parse_form()
            truck = conn.execute(
                "SELECT registration_number FROM trucks WHERE truck_id = ?",
                (data["truck_id"],),
            ).fetchone()
            if not truck:
                raise ValueError("The selected truck does not exist.")
            data["owner_user_id"] = session["user_id"]
            data["created_by"] = session["user_id"]
            conn.execute(
                "INSERT INTO challans (truck_id, challan_number, violation, issue_date, due_date, "
                "amount, status, issuing_authority, location, payment_date, notes, owner_user_id, "
                "created_by, created_at, updated_at) VALUES (:truck_id, :challan_number, :violation, "
                ":issue_date, :due_date, :amount, :status, :issuing_authority, :location, :payment_date, "
                ":notes, :owner_user_id, :created_by, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                data,
            )
            _record_user_activity(
                conn, "Traffic Challan Added",
                f"Challan {data['challan_number']} for {truck['registration_number']}: "
                f"{data['status']}, fine ₹{data['amount']:.2f}, due {data['due_date'] or 'not set'}."
            )
            conn.commit()
            flash("Traffic challan saved.", "success")
        except ValueError as error:
            conn.rollback()
            flash(str(error), "error")
        except Exception as error:
            conn.rollback()
            if "UNIQUE constraint failed" in str(error):
                flash("This challan number already exists for your records.", "error")
            else:
                app.logger.exception("Unable to create challan record.")
                flash("Could not save the challan. Check the details and try again.", "error")
        finally:
            conn.close()
        return redirect(url_for("challans"))

    @app.route("/challans/<int:challan_id>/edit", methods=["POST"], endpoint="challan_edit")
    @admin_required
    def challan_edit(challan_id):
        if not _valid_csrf_token():
            flash("Your session token is invalid. Reload the page and try again.", "error")
            return redirect(url_for("challans"))
        conn = get_db()
        try:
            old = scoped_challan(conn, challan_id)
            if not old:
                abort(404)
            data = parse_form()
            truck = conn.execute(
                "SELECT registration_number FROM trucks WHERE truck_id = ?",
                (data["truck_id"],),
            ).fetchone()
            if not truck:
                raise ValueError("The selected truck does not exist.")
            conn.execute(
                "UPDATE challans SET truck_id=:truck_id, challan_number=:challan_number, "
                "violation=:violation, issue_date=:issue_date, due_date=:due_date, amount=:amount, "
                "status=:status, issuing_authority=:issuing_authority, location=:location, "
                "payment_date=:payment_date, notes=:notes, updated_at=CURRENT_TIMESTAMP "
                "WHERE challan_id=:challan_id",
                {**data, "challan_id": challan_id},
            )
            _record_user_activity(
                conn, "Traffic Challan Updated",
                f"Updated challan {data['challan_number']} for {truck['registration_number']}."
            )
            conn.commit()
            flash("Traffic challan updated.", "success")
        except ValueError as error:
            conn.rollback()
            flash(str(error), "error")
        except Exception as error:
            conn.rollback()
            if "UNIQUE constraint failed" in str(error):
                flash("This challan number already exists for your records.", "error")
            else:
                app.logger.exception("Unable to update challan record.")
                flash("Could not update the challan. Check the details and try again.", "error")
        finally:
            conn.close()
        return redirect(url_for("challans"))

    @app.route("/challans/<int:challan_id>/delete", methods=["POST"], endpoint="challan_delete")
    @admin_required
    def challan_delete(challan_id):
        if not _valid_csrf_token():
            flash("Your session token is invalid. Reload the page and try again.", "error")
            return redirect(url_for("challans"))
        conn = get_db()
        try:
            row = scoped_challan(conn, challan_id)
            if not row:
                abort(404)
            conn.execute("DELETE FROM challans WHERE challan_id = ?", (challan_id,))
            _record_user_activity(
                conn, "Traffic Challan Deleted",
                f"Deleted challan {row['challan_number']}."
            )
            conn.commit()
            flash("Traffic challan deleted.", "success")
        except Exception:
            conn.rollback()
            app.logger.exception("Unable to delete challan record.")
            flash("Could not delete the challan record.", "error")
        finally:
            conn.close()
        return redirect(url_for("challans"))
