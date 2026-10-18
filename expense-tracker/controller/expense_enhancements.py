"""Secure expense-management enhancements for FleetFlow.

This module is registered after the application's original routes so the
expense screens can be enhanced without disrupting the rest of the fleet app.
"""
import csv
import io
import os
import re
import secrets
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from flask import Response, abort, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.utils import secure_filename


CATEGORIES = {
    "Fuel",
    "Toll",
    "Meals/Allowance",
    "Maintenance/Repairs",
    "Loading/Unloading",
    "Other",
    "Food",
    "Maintenance",
}
STATUSES = {"Approved", "Pending", "Rejected"}
MAX_RECEIPT_SIZE = 10 * 1024 * 1024
RECEIPT_SIGNATURES = {
    ".pdf": (b"%PDF-",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
}


def register_expense_enhancements(app, ns):
    """Register hardened expense screens and endpoints against the existing app."""
    get_db = ns["get_db"]
    access_required = ns["access_required"]
    admin_required = ns["admin_required"]
    _is_admin_user = ns["_is_admin_user"]
    _record_owner_filter = ns["_record_owner_filter"]
    _valid_csrf_token = ns["_valid_csrf_token"]
    _get_trip_financials = ns["_get_trip_financials"]
    SUPER_ADMIN_EMAIL = ns["SUPER_ADMIN_EMAIL"]
    app_logger = app.logger

    def expense_trip_scope():
        # Admins manage the shared fleet ledger, including trips created by
        # other administrators. Non-admin users remain restricted to trips
        # assigned to their linked driver record.
        if _is_admin_user():
            return "1 = 1", []
        return _record_owner_filter("trips")

    def get_filters():
        selected_trip_id = request.args.get("trip", type=int)
        selected_status = request.args.get("status", "")
        selected_category = request.args.get("category", "")
        date_from = request.args.get("date_from", "").strip()
        date_to = request.args.get("date_to", "").strip()

        if selected_status not in STATUSES:
            selected_status = ""
        if selected_category not in CATEGORIES:
            selected_category = ""

        for name, value in (("start", date_from), ("end", date_to)):
            if value:
                try:
                    parsed = date.fromisoformat(value)
                    if parsed.isoformat() != value:
                        raise ValueError
                except ValueError:
                    if name == "start":
                        date_from = ""
                    else:
                        date_to = ""
        if date_from and date_to and date_from > date_to:
            date_from, date_to = date_to, date_from

        return {
            "trip_id": selected_trip_id,
            "status": selected_status,
            "category": selected_category,
            "date_from": date_from,
            "date_to": date_to,
        }

    def build_where(filters):
        scope_sql, scope_params = expense_trip_scope()
        clauses = [scope_sql]
        params = list(scope_params)
        if filters["trip_id"] is not None:
            clauses.append("expenses.trip_id = ?")
            params.append(filters["trip_id"])
        if filters["status"]:
            clauses.append("expenses.status = ?")
            params.append(filters["status"])
        if filters["category"]:
            clauses.append("expenses.category = ?")
            params.append(filters["category"])
        if filters["date_from"]:
            clauses.append("expenses.date >= ?")
            params.append(filters["date_from"])
        if filters["date_to"]:
            clauses.append("expenses.date <= ?")
            params.append(filters["date_to"])
        return "WHERE " + " AND ".join(clauses), params

    def receipt_directory():
        configured = os.environ.get("FLEETFLOW_UPLOAD_DIR", "").strip()
        if configured:
            root = Path(configured).expanduser()
        elif Path("/app/uploads").is_dir():
            # Docker Compose mounts this directory as a persistent volume.
            root = Path("/app/uploads")
        else:
            root = Path(app.instance_path) / "uploads"
        target = root / "expense_receipts"
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(target, 0o700)
        except OSError:
            app_logger.warning("Could not tighten permissions on receipt directory %s", target)
        return target.resolve()

    def store_receipt(upload):
        if not upload or not upload.filename:
            return None, None, None, None
        original_name = secure_filename(upload.filename or "")
        suffix = Path(original_name).suffix.lower()
        if not original_name or suffix not in RECEIPT_SIGNATURES:
            raise ValueError("Receipt must be a PDF, JPG, JPEG, or PNG file.")
        content = upload.stream.read(MAX_RECEIPT_SIZE + 1)
        if not content:
            raise ValueError("The receipt file is empty.")
        if len(content) > MAX_RECEIPT_SIZE:
            raise ValueError("Each receipt must be 10 MB or smaller.")
        if not any(content.startswith(signature) for signature in RECEIPT_SIGNATURES[suffix]):
            raise ValueError("The receipt contents do not match the selected file type.")

        storage_name = f"{secrets.token_hex(24)}{suffix}"
        directory = receipt_directory()
        path = directory / storage_name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return storage_name, original_name, len(content), path

    def audit(conn, owner_user_id, action, details):
        conn.execute(
            "INSERT INTO user_logs (user_id, action, details, actor_user_id) VALUES (?, ?, ?, ?)",
            (owner_user_id or session["user_id"], action, details, session["user_id"]),
        )

    def query_expenses(conn, where_sql, params):
        return conn.execute(
            "SELECT expenses.expense_id, expenses.trip_id, expenses.category, "
            "expenses.amount, expenses.date, expenses.description, expenses.status, "
            "expenses.reviewed_at, expenses.review_note, expenses.created_by, "
            "expenses.receipt_storage_name, expenses.receipt_original_name, expenses.receipt_size, "
            "trips.route_from, trips.route_to, trips.owner_user_id, "
            "trucks.registration_number, drivers.name AS driver_name, "
            "submitter.full_name AS submitter_name, submitter.email AS submitter_email, "
            "reviewer.full_name AS reviewer_name, reviewer.email AS reviewer_email "
            "FROM expenses JOIN trips ON trips.trip_id = expenses.trip_id "
            "LEFT JOIN trucks ON trucks.truck_id = trips.truck_id "
            "LEFT JOIN drivers ON drivers.driver_id = trips.driver_id "
            "LEFT JOIN users AS submitter ON submitter.user_id = expenses.created_by "
            "LEFT JOIN users AS reviewer ON reviewer.user_id = expenses.reviewed_by "
            f"{where_sql} "
            "ORDER BY COALESCE(expenses.created_at, expenses.date) DESC, expenses.expense_id DESC",
            params,
        ).fetchall()

    def enhanced_expenses():
        session.setdefault("csrf_token", secrets.token_urlsafe(32))
        filters = get_filters()
        where_sql, params = build_where(filters)
        is_admin = _is_admin_user()
        conn = get_db()
        try:
            rows = query_expenses(conn, where_sql, params)
            record_ids = [row["expense_id"] for row in rows]
            corrections_by_expense = {}
            if record_ids:
                placeholders = ",".join("?" for _ in record_ids)
                corrections = conn.execute(
                    "SELECT financial_corrections.correction_id, financial_corrections.record_id, "
                    "financial_corrections.old_amount, financial_corrections.new_amount, "
                    "financial_corrections.old_status, financial_corrections.new_status, "
                    "financial_corrections.reason, financial_corrections.changed_at, "
                    "users.full_name AS changed_by_name, users.email AS changed_by_email "
                    "FROM financial_corrections LEFT JOIN users "
                    "ON users.user_id = financial_corrections.changed_by "
                    "WHERE financial_corrections.record_type = 'Expense' "
                    f"AND financial_corrections.record_id IN ({placeholders}) "
                    "ORDER BY financial_corrections.correction_id DESC",
                    record_ids,
                ).fetchall()
                for correction in corrections:
                    corrections_by_expense.setdefault(correction["record_id"], []).append(dict(correction))

            expense_rows = []
            expenses_by_trip = {}
            filtered_expense_total = Decimal("0.00")
            for row in rows:
                expense = dict(row)
                expense["corrections"] = corrections_by_expense.get(expense["expense_id"], [])
                expense["can_correct"] = is_admin or expense["created_by"] == session["user_id"]
                expense_rows.append(expense)

                amount = Decimal(str(expense["amount"] or 0)).quantize(Decimal("0.01"))
                filtered_expense_total += amount
                trip_id = expense["trip_id"]
                group = expenses_by_trip.get(trip_id)
                if group is None:
                    group = {
                        "trip_id": trip_id,
                        "route_from": expense["route_from"],
                        "route_to": expense["route_to"],
                        "registration_number": expense["registration_number"],
                        "driver_name": expense["driver_name"],
                        "expenses": [],
                        "total": Decimal("0.00"),
                        "approved_total": Decimal("0.00"),
                        "pending_total": Decimal("0.00"),
                        "rejected_total": Decimal("0.00"),
                    }
                    expenses_by_trip[trip_id] = group
                group["expenses"].append(expense)
                group["total"] += amount
                if expense["status"] == "Approved":
                    group["approved_total"] += amount
                elif expense["status"] == "Pending":
                    group["pending_total"] += amount
                elif expense["status"] == "Rejected":
                    group["rejected_total"] += amount

            totals = conn.execute(
                "SELECT COUNT(*) AS count, "
                "COALESCE(SUM(CASE WHEN expenses.status = 'Pending' THEN expenses.amount ELSE 0 END), 0) AS pending, "
                "COALESCE(SUM(CASE WHEN expenses.status = 'Approved' THEN expenses.amount ELSE 0 END), 0) AS approved, "
                "COALESCE(SUM(CASE WHEN expenses.status = 'Rejected' THEN expenses.amount ELSE 0 END), 0) AS rejected "
                "FROM expenses JOIN trips ON trips.trip_id = expenses.trip_id "
                f"{where_sql}",
                params,
            ).fetchone()

            category_summary = conn.execute(
                "SELECT expenses.category, COUNT(*) AS count, "
                "COALESCE(SUM(expenses.amount), 0) AS total, "
                "COALESCE(SUM(CASE WHEN expenses.status = 'Approved' THEN expenses.amount ELSE 0 END), 0) AS approved_total, "
                "COALESCE(SUM(CASE WHEN expenses.status = 'Pending' THEN expenses.amount ELSE 0 END), 0) AS pending_total "
                "FROM expenses JOIN trips ON trips.trip_id = expenses.trip_id "
                f"{where_sql} GROUP BY expenses.category ORDER BY total DESC, expenses.category",
                params,
            ).fetchall()

            finance_summary, trip_finances = _get_trip_financials(
                conn, None if is_admin else "scoped"
            )
            scope_sql, scope_params = expense_trip_scope()
            trips = conn.execute(
                "SELECT trips.trip_id, trips.route_from, trips.route_to, "
                "trucks.registration_number, drivers.name AS driver_name "
                "FROM trips LEFT JOIN trucks ON trucks.truck_id = trips.truck_id "
                "LEFT JOIN drivers ON drivers.driver_id = trips.driver_id "
                f"WHERE trips.status != 'Cancelled' AND {scope_sql} "
                "ORDER BY trips.trip_id DESC",
                scope_params,
            ).fetchall()
        finally:
            conn.close()

        return render_template(
            "expenses.html",
            expenses=expense_rows,
            expenses_by_trip=list(expenses_by_trip.values()),
            selected_trip_id=filters["trip_id"],
            selected_status=filters["status"],
            selected_category=filters["category"],
            date_from=filters["date_from"],
            date_to=filters["date_to"],
            filtered_expense_total=filtered_expense_total,
            totals=totals,
            category_summary=category_summary,
            finance_summary=finance_summary,
            trip_finances=trip_finances,
            trips=trips,
            today=date.today().isoformat(),
            csrf_token=session["csrf_token"],
            can_review=is_admin,
            block_content="dashboard",
        )

    def enhanced_create_expense():
        if not _valid_csrf_token():
            flash("Your session token is invalid. Reload the expenses page and try again.", "error")
            return redirect(url_for("expenses"))

        try:
            trip_id = int(request.form.get("trip_id", ""))
            amount = Decimal(request.form.get("amount", "").strip()).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            expense_date = request.form.get("date", "").strip()
            parsed_date = date.fromisoformat(expense_date)
            if parsed_date.isoformat() != expense_date:
                raise ValueError
        except (TypeError, ValueError, InvalidOperation):
            flash("Enter a valid trip, date, and amount.", "error")
            return redirect(url_for("expenses"))

        category = request.form.get("category", "").strip()
        description = request.form.get("description", "").strip()
        if category not in CATEGORIES:
            flash("Choose a valid expense category.", "error")
            return redirect(url_for("expenses"))
        if not amount.is_finite() or not Decimal("0") < amount <= Decimal("100000000"):
            flash("Amount must be greater than zero and no more than 100,000,000.", "error")
            return redirect(url_for("expenses"))
        if len(description) > 500:
            flash("Notes must be 500 characters or fewer.", "error")
            return redirect(url_for("expenses"))

        conn = get_db()
        receipt_path = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            scope_sql, scope_params = expense_trip_scope()
            trip = conn.execute(
                "SELECT trips.trip_id, trips.owner_user_id FROM trips "
                "WHERE trips.trip_id = ? AND trips.status != 'Cancelled' "
                f"AND {scope_sql}",
                [trip_id, *scope_params],
            ).fetchone()
            if not trip:
                conn.rollback()
                flash("Choose a trip you are authorized to access that is not cancelled.", "error")
                return redirect(url_for("expenses"))

            receipt_storage_name, receipt_original_name, receipt_size, receipt_path = store_receipt(
                request.files.get("receipt")
            )
            is_admin = _is_admin_user()
            status = "Approved" if is_admin else "Pending"
            reviewer_id = session["user_id"] if is_admin else None
            cursor = conn.execute(
                "INSERT INTO expenses "
                "(trip_id, category, amount, date, description, created_at, created_by, status, "
                "reviewed_by, reviewed_at, review_note, receipt_storage_name, receipt_original_name, receipt_size) "
                "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, "
                "CASE WHEN ? = 'Approved' THEN CURRENT_TIMESTAMP ELSE NULL END, NULL, ?, ?, ?)",
                (
                    trip_id, category, float(amount), expense_date, description or None,
                    session["user_id"], status, reviewer_id, status,
                    receipt_storage_name, receipt_original_name, receipt_size,
                ),
            )
            expense_id = cursor.lastrowid
            audit(
                conn,
                trip["owner_user_id"],
                "Expense Recorded" if is_admin else "Expense Submitted",
                f"Expense #{expense_id} recorded for trip #{trip_id}: {category}, "
                f"{amount:.2f}, status {status.lower()}"
                + (", receipt attached." if receipt_storage_name else "."),
            )
            conn.commit()
        except ValueError as error:
            conn.rollback()
            if receipt_path:
                receipt_path.unlink(missing_ok=True)
            flash(str(error), "error")
            return redirect(url_for("expenses"))
        except Exception:
            conn.rollback()
            if receipt_path:
                receipt_path.unlink(missing_ok=True)
            app_logger.exception("Failed to save an expense record.")
            flash("The expense could not be saved. No expense record was created.", "error")
            return redirect(url_for("expenses"))
        finally:
            conn.close()

        flash(
            f"Expense #{expense_id} recorded and approved."
            if is_admin else f"Expense #{expense_id} submitted for admin review.",
            "success",
        )
        return redirect(url_for("expenses"))

    def enhanced_review_expense(id):
        if not _valid_csrf_token():
            flash("Your session token is invalid. Reload the expenses page and try again.", "error")
            return redirect(url_for("expenses"))

        decision = request.form.get("status", "")
        if decision not in {"Approved", "Rejected"}:
            flash("Choose approve or reject.", "error")
            return redirect(url_for("expenses"))
        review_note = request.form.get("review_note", "").strip()
        if decision == "Rejected" and not 5 <= len(review_note) <= 300:
            flash("A rejection reason of 5 to 300 characters is required.", "error")
            return redirect(url_for("expenses"))
        if decision == "Approved" and len(review_note) > 300:
            flash("Review notes must be 300 characters or fewer.", "error")
            return redirect(url_for("expenses"))

        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            scope_sql, scope_params = expense_trip_scope()
            expense = conn.execute(
                "SELECT expenses.trip_id, expenses.category, expenses.amount, trips.owner_user_id "
                "FROM expenses JOIN trips ON trips.trip_id = expenses.trip_id "
                "WHERE expenses.expense_id = ? AND expenses.status = 'Pending' "
                f"AND {scope_sql}",
                [id, *scope_params],
            ).fetchone()
            if not expense:
                conn.rollback()
                flash("This expense is no longer awaiting review or is outside your access.", "error")
                return redirect(url_for("expenses"))

            updated = conn.execute(
                "UPDATE expenses SET status = ?, reviewed_by = ?, "
                "reviewed_at = CURRENT_TIMESTAMP, review_note = ? "
                "WHERE expense_id = ? AND status = 'Pending'",
                (decision, session["user_id"], review_note or None, id),
            )
            if updated.rowcount != 1:
                conn.rollback()
                flash("This expense is no longer awaiting review.", "error")
                return redirect(url_for("expenses"))

            audit(
                conn,
                expense["owner_user_id"],
                "Expense Reviewed",
                f"Expense #{id} for trip #{expense['trip_id']} ({expense['category']}, "
                f"{float(expense['amount']):.2f}) was {decision.lower()}."
                + (f" Note: {review_note}" if review_note else ""),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            app_logger.exception("Failed to review expense #%s.", id)
            flash("The expense review could not be saved.", "error")
            return redirect(url_for("expenses"))
        finally:
            conn.close()

        flash(f"Expense #{id} {decision.lower()}.", "success")
        return redirect(url_for("expenses", status=decision))

    def enhanced_correct_expense_amount(id):
        if not _valid_csrf_token():
            flash("Your session token is invalid. Reload the expenses page and try again.", "error")
            return redirect(url_for("expenses"))

        try:
            new_amount = Decimal(request.form.get("amount", "").strip()).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        except (TypeError, InvalidOperation):
            flash("Enter a valid corrected amount.", "error")
            return redirect(url_for("expenses"))
        if not new_amount.is_finite() or not Decimal("0") < new_amount <= Decimal("100000000"):
            flash("Corrected amount must be greater than zero and no more than 100,000,000.", "error")
            return redirect(url_for("expenses"))

        reason = request.form.get("reason", "").strip()
        if not 5 <= len(reason) <= 300:
            flash("Explain the correction in 5 to 300 characters.", "error")
            return redirect(url_for("expenses"))

        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            scope_sql, scope_params = expense_trip_scope()
            expense = conn.execute(
                "SELECT expenses.trip_id, expenses.amount, expenses.status, expenses.created_by, "
                "trips.owner_user_id FROM expenses JOIN trips ON trips.trip_id = expenses.trip_id "
                f"WHERE expenses.expense_id = ? AND {scope_sql}",
                [id, *scope_params],
            ).fetchone()
            is_admin = _is_admin_user()
            if not expense or not (is_admin or expense["created_by"] == session["user_id"]):
                conn.rollback()
                flash("Only the submitter or an admin can correct this expense.", "error")
                return redirect(url_for("expenses"))

            old_amount = Decimal(str(expense["amount"])).quantize(Decimal("0.01"))
            if old_amount == new_amount:
                conn.rollback()
                flash("The corrected amount must differ from the current amount.", "error")
                return redirect(url_for("expenses"))

            old_status = expense["status"]
            new_status = "Approved" if is_admin else "Pending"
            reviewer_id = session["user_id"] if is_admin else None
            conn.execute(
                "INSERT INTO financial_corrections "
                "(record_type, record_id, old_amount, new_amount, old_status, new_status, reason, changed_by) "
                "VALUES ('Expense', ?, ?, ?, ?, ?, ?, ?)",
                (
                    id, float(old_amount), float(new_amount), old_status,
                    new_status, reason, session["user_id"],
                ),
            )
            conn.execute(
                "UPDATE expenses SET amount = ?, status = ?, reviewed_by = ?, "
                "reviewed_at = CASE WHEN ? = 'Approved' THEN CURRENT_TIMESTAMP ELSE NULL END, "
                "review_note = NULL WHERE expense_id = ?",
                (float(new_amount), new_status, reviewer_id, new_status, id),
            )
            audit(
                conn,
                expense["owner_user_id"],
                "Expense Amount Corrected",
                f"Corrected expense #{id} on trip #{expense['trip_id']} from "
                f"{old_amount:.2f} to {new_amount:.2f}. Reason: {reason}",
            )
            conn.commit()
        except Exception:
            conn.rollback()
            app_logger.exception("Failed to correct expense #%s.", id)
            flash("The expense correction could not be saved.", "error")
            return redirect(url_for("expenses"))
        finally:
            conn.close()

        flash(
            f"Expense #{id} corrected and approved."
            if is_admin else f"Expense #{id} corrected and sent for admin review.",
            "success",
        )
        return redirect(url_for("expenses"))

    def download_expense_receipt(id):
        if not _valid_receipt_id(id):
            abort(404)
        conn = get_db()
        try:
            scope_sql, scope_params = expense_trip_scope()
            expense = conn.execute(
                "SELECT expenses.created_by, expenses.receipt_storage_name, "
                "expenses.receipt_original_name, trips.owner_user_id "
                "FROM expenses JOIN trips ON trips.trip_id = expenses.trip_id "
                f"WHERE expenses.expense_id = ? AND {scope_sql}",
                [id, *scope_params],
            ).fetchone()
            if not expense or (
                not _is_admin_user() and expense["created_by"] != session["user_id"]
            ):
                abort(404)
            storage_name = expense["receipt_storage_name"]
            original_name = expense["receipt_original_name"]
            if not storage_name or not original_name:
                abort(404)
            if not re.fullmatch(r"[a-f0-9]{48}\.(pdf|jpg|jpeg|png)", storage_name):
                abort(404)
            directory = receipt_directory()
            path = (directory / storage_name).resolve()
            if path.parent != directory or not path.is_file():
                abort(404)
            audit(
                conn,
                expense["owner_user_id"],
                "Expense Receipt Downloaded",
                f"Downloaded receipt for expense #{id}.",
            )
            conn.commit()
        finally:
            conn.close()

        response = send_file(
            path,
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=original_name,
            conditional=True,
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def _valid_receipt_id(expense_id):
        return isinstance(expense_id, int) and expense_id > 0

    def export_expenses_csv():
        filters = get_filters()
        where_sql, params = build_where(filters)
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT expenses.expense_id, expenses.trip_id, expenses.date, expenses.category, "
                "expenses.amount, expenses.status, expenses.description, expenses.review_note, "
                "expenses.receipt_original_name, drivers.name AS driver_name, "
                "trucks.registration_number, trips.route_from, trips.route_to, "
                "submitter.full_name AS submitter_name, submitter.email AS submitter_email, "
                "reviewer.full_name AS reviewer_name, reviewer.email AS reviewer_email, "
                "expenses.reviewed_at "
                "FROM expenses JOIN trips ON trips.trip_id = expenses.trip_id "
                "LEFT JOIN drivers ON drivers.driver_id = trips.driver_id "
                "LEFT JOIN trucks ON trucks.truck_id = trips.truck_id "
                "LEFT JOIN users AS submitter ON submitter.user_id = expenses.created_by "
                "LEFT JOIN users AS reviewer ON reviewer.user_id = expenses.reviewed_by "
                f"{where_sql} "
                "ORDER BY expenses.date DESC, expenses.expense_id DESC",
                params,
            ).fetchall()
        finally:
            conn.close()

        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow([
            "Expense ID", "Trip ID", "Expense Date", "Category", "Amount",
            "Status", "Description", "Review Note / Rejection Reason",
            "Receipt File", "Driver", "Truck", "Route From", "Route To",
            "Submitted By", "Reviewed By", "Reviewed At",
        ])

        def safe_cell(value):
            text = "" if value is None else str(value)
            # Reduce spreadsheet formula injection risk when opening CSV exports.
            if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
                return "'" + text
            return text

        for row in rows:
            writer.writerow([
                safe_cell(row["expense_id"]), safe_cell(row["trip_id"]),
                safe_cell(row["date"]), safe_cell(row["category"]),
                safe_cell(f"{float(row['amount']):.2f}"), safe_cell(row["status"]),
                safe_cell(row["description"]), safe_cell(row["review_note"]),
                safe_cell(row["receipt_original_name"]), safe_cell(row["driver_name"]),
                safe_cell(row["registration_number"]), safe_cell(row["route_from"]),
                safe_cell(row["route_to"]),
                safe_cell(row["submitter_name"] or row["submitter_email"]),
                safe_cell(row["reviewer_name"] or row["reviewer_email"]),
                safe_cell(row["reviewed_at"]),
            ])
        response = Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = (
            f"attachment; filename=fleetflow-expenses-{date.today().isoformat()}.csv"
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response

    app.view_functions["expenses"] = access_required(enhanced_expenses)
    app.view_functions["create_expense"] = access_required(enhanced_create_expense)
    app.view_functions["correct_expense_amount"] = access_required(enhanced_correct_expense_amount)
    app.view_functions["review_expense"] = admin_required(enhanced_review_expense)
    app.add_url_rule(
        "/expenses/<int:id>/receipt",
        endpoint="download_expense_receipt",
        view_func=access_required(download_expense_receipt),
        methods=["GET"],
    )
    app.add_url_rule(
        "/expenses/export.csv",
        endpoint="export_expenses_csv",
        view_func=access_required(export_expenses_csv),
        methods=["GET"],
    )
