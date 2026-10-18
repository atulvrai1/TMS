"""Full maintenance management for FleetFlow.

This module extends the legacy maintenance table in place, preserving existing
records while adding scheduling, status, odometer and cost breakdown fields.
"""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import abort, flash, redirect, render_template, request, session, url_for


STATUSES = {"Scheduled", "In Progress", "Completed"}
MAX_TEXT = 500


def _money(value, label):
    try:
        amount = Decimal((value or "0").strip()).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, AttributeError):
        raise ValueError(f"{label} must be a valid amount.")
    if not amount.is_finite() or amount < 0 or amount > Decimal("999999999.99"):
        raise ValueError(f"{label} must be between 0 and 999,999,999.99.")
    return float(amount)


def _optional_int(value, label):
    value = (value or "").strip()
    if not value:
        return None
    if not value.isdigit():
        raise ValueError(f"{label} must be a non-negative whole number.")
    parsed = int(value)
    if parsed > 2_000_000_000:
        raise ValueError(f"{label} is too large.")
    return parsed


def _optional_date(value, label):
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} must be a valid date.")
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a valid date.")
    return value


def _ensure_schema(get_db):
    """Add new fields safely; do not recreate or discard the legacy table."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS maintenance (
                maintenance_id INTEGER PRIMARY KEY AUTOINCREMENT,
                truck_id INTEGER NOT NULL,
                service_date DATE NOT NULL,
                service_type TEXT NOT NULL,
                cost REAL NOT NULL DEFAULT 0,
                notes TEXT,
                owner_user_id INTEGER REFERENCES users(user_id),
                FOREIGN KEY (truck_id) REFERENCES trucks(truck_id)
            )
        """)
        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(maintenance)")
        }
        additions = {
            "odometer_reading": "INTEGER",
            "next_service_date": "DATE",
            "next_service_odometer": "INTEGER",
            "vendor": "TEXT",
            "parts_cost": "REAL NOT NULL DEFAULT 0",
            "labour_cost": "REAL NOT NULL DEFAULT 0",
            "tax_cost": "REAL NOT NULL DEFAULT 0",
            "status": "TEXT NOT NULL DEFAULT 'Completed'",
            "created_by": "INTEGER REFERENCES users(user_id)",
            "created_at": "DATETIME",
            "updated_at": "DATETIME",
        }
        for column, definition in additions.items():
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE maintenance ADD COLUMN {column} {definition}"
                )
        conn.execute(
            "UPDATE maintenance SET created_at = CURRENT_TIMESTAMP "
            "WHERE created_at IS NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_maintenance_truck_date "
            "ON maintenance(truck_id, service_date DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_maintenance_schedule "
            "ON maintenance(status, next_service_date, next_service_odometer)"
        )


def register_maintenance_module(app, ns):
    get_db = ns["get_db"]
    admin_required = ns["admin_required"]
    _is_admin_user = ns["_is_admin_user"]
    _record_owner_filter = ns["_record_owner_filter"]
    _valid_csrf_token = ns["_valid_csrf_token"]
    _record_user_activity = ns["_record_user_activity"]

    _ensure_schema(get_db)

    def require_csrf():
        if not _valid_csrf_token():
            flash("Your session token is invalid. Reload the page and try again.", "error")
            return False
        return True

    def scoped_record(conn, maintenance_id):
        scope_sql, scope_params = _record_owner_filter("maintenance")
        return conn.execute(
            "SELECT maintenance.* FROM maintenance "
            f"WHERE maintenance.maintenance_id = ? AND ({scope_sql})",
            [maintenance_id, *scope_params],
        ).fetchone()

    def refresh_truck_service_state(conn, truck_id):
        latest = conn.execute(
            "SELECT service_date FROM maintenance "
            "WHERE truck_id = ? AND status = 'Completed' "
            "ORDER BY service_date DESC, maintenance_id DESC LIMIT 1",
            (truck_id,),
        ).fetchone()
        conn.execute(
            "UPDATE trucks SET last_service_date = ? WHERE truck_id = ?",
            (latest["service_date"] if latest else None, truck_id),
        )
        active = conn.execute(
            "SELECT 1 FROM maintenance WHERE truck_id = ? "
            "AND status = 'In Progress' LIMIT 1",
            (truck_id,),
        ).fetchone()
        truck = conn.execute(
            "SELECT status FROM trucks WHERE truck_id = ?", (truck_id,)
        ).fetchone()
        if truck and truck["status"] == "Maintenance" and not active:
            conn.execute(
                "UPDATE trucks SET status = 'Available' WHERE truck_id = ?",
                (truck_id,),
            )
        elif active:
            conn.execute(
                "UPDATE trucks SET status = 'Maintenance' WHERE truck_id = ? "
                "AND status != 'In Trip'",
                (truck_id,),
            )

    def read_form():
        service_type = request.form.get("service_type", "").strip()
        status = request.form.get("status", "Scheduled").strip()
        vendor = request.form.get("vendor", "").strip()
        notes = request.form.get("notes", "").strip()
        if not service_type or len(service_type) > 100:
            raise ValueError("Enter a service type (up to 100 characters).")
        if status not in STATUSES:
            raise ValueError("Choose a valid maintenance status.")
        if len(vendor) > 120:
            raise ValueError("Workshop/vendor must be 120 characters or fewer.")
        if len(notes) > MAX_TEXT:
            raise ValueError("Notes must be 500 characters or fewer.")

        truck_raw = request.form.get("truck_id", "").strip()
        if not truck_raw.isdigit():
            raise ValueError("Select a valid truck.")
        service_date = _optional_date(request.form.get("service_date"), "Service date")
        if not service_date:
            raise ValueError("Service date is required.")

        odometer = _optional_int(request.form.get("odometer_reading"), "Odometer")
        next_odometer = _optional_int(
            request.form.get("next_service_odometer"), "Next service odometer"
        )
        next_date = _optional_date(
            request.form.get("next_service_date"), "Next service date"
        )
        if odometer is not None and next_odometer is not None and next_odometer < odometer:
            raise ValueError("Next service odometer cannot be below the current odometer.")

        parts = _money(request.form.get("parts_cost"), "Parts cost")
        labour = _money(request.form.get("labour_cost"), "Labour cost")
        tax = _money(request.form.get("tax_cost"), "Tax")
        total = _money(str(Decimal(str(parts)) + Decimal(str(labour)) + Decimal(str(tax))), "Total cost")
        return {
            "truck_id": int(truck_raw),
            "service_date": service_date,
            "service_type": service_type,
            "status": status,
            "vendor": vendor or None,
            "notes": notes or None,
            "odometer_reading": odometer,
            "next_service_date": next_date,
            "next_service_odometer": next_odometer,
            "parts_cost": parts,
            "labour_cost": labour,
            "tax_cost": tax,
            "cost": total,
        }

    def list_maintenance():
        if not _is_admin_user():
            abort(403)
        session.setdefault("csrf_token", __import__("secrets").token_urlsafe(32))
        conn = get_db()
        scope_sql, scope_params = _record_owner_filter("maintenance")
        status_filter = request.args.get("status", "").strip()
        if status_filter not in STATUSES:
            status_filter = ""
        search = request.args.get("q", "").strip()[:100]
        clauses = [scope_sql]
        params = list(scope_params)
        if status_filter:
            clauses.append("maintenance.status = ?")
            params.append(status_filter)
        if search:
            clauses.append(
                "(trucks.registration_number LIKE ? OR maintenance.service_type LIKE ? "
                "OR COALESCE(maintenance.vendor, '') LIKE ? OR COALESCE(maintenance.notes, '') LIKE ?)"
            )
            pattern = f"%{search}%"
            params.extend([pattern, pattern, pattern, pattern])
        where = " AND ".join(clauses)
        rows = conn.execute(
            "SELECT maintenance.*, trucks.registration_number, trucks.model, "
            "trucks.status AS truck_status FROM maintenance "
            "JOIN trucks ON trucks.truck_id = maintenance.truck_id "
            f"WHERE {where} ORDER BY "
            "CASE maintenance.status WHEN 'In Progress' THEN 0 WHEN 'Scheduled' THEN 1 ELSE 2 END, "
            "COALESCE(maintenance.next_service_date, maintenance.service_date) ASC, "
            "maintenance.maintenance_id DESC",
            params,
        ).fetchall()
        trucks = conn.execute(
            "SELECT truck_id, registration_number, model, status FROM trucks "
            "ORDER BY registration_number COLLATE NOCASE"
        ).fetchall()
        summary = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(cost), 0) AS total_cost, "
            "SUM(CASE WHEN status = 'Scheduled' THEN 1 ELSE 0 END) AS scheduled, "
            "SUM(CASE WHEN status = 'In Progress' THEN 1 ELSE 0 END) AS in_progress, "
            "SUM(CASE WHEN status = 'Completed' THEN 1 ELSE 0 END) AS completed "
            f"FROM maintenance WHERE {scope_sql}",
            scope_params,
        ).fetchone()
        today = date.today().isoformat()
        due_soon = conn.execute(
            "SELECT COUNT(*) AS count FROM maintenance "
            f"WHERE {scope_sql} AND status != 'Completed' "
            "AND next_service_date IS NOT NULL "
            "AND next_service_date BETWEEN ? AND date(?, '+30 days')",
            [*scope_params, today, today],
        ).fetchone()["count"]
        overdue = conn.execute(
            "SELECT COUNT(*) AS count FROM maintenance "
            f"WHERE {scope_sql} AND status != 'Completed' AND next_service_date < ?",
            [*scope_params, today],
        ).fetchone()["count"]
        conn.close()
        return render_template(
            "maintenance.html",
            maintenance_records=rows,
            trucks=trucks,
            summary=summary,
            due_soon=due_soon,
            overdue=overdue,
            selected_status=status_filter,
            search_query=search,
            today=today,
            csrf_token=session["csrf_token"],
            block_content="dashboard",
        )

    @app.route("/maintenance/add", methods=["POST"], endpoint="maintenance_add")
    @admin_required
    def maintenance_add():
        if not require_csrf():
            return redirect(url_for("maintenance"))
        conn = get_db()
        try:
            data = read_form()
            truck = conn.execute(
                "SELECT truck_id, registration_number, status FROM trucks WHERE truck_id = ?",
                (data["truck_id"],),
            ).fetchone()
            if not truck:
                raise ValueError("The selected truck does not exist.")
            if truck["status"] == "In Trip" and data["status"] == "In Progress":
                raise ValueError("A truck marked In Trip cannot start maintenance.")
            data["owner_user_id"] = session["user_id"]
            data["created_by"] = session["user_id"]
            conn.execute(
                "INSERT INTO maintenance "
                "(truck_id, service_date, service_type, cost, notes, owner_user_id, "
                "odometer_reading, next_service_date, next_service_odometer, vendor, "
                "parts_cost, labour_cost, tax_cost, status, created_by, created_at, updated_at) "
                "VALUES (:truck_id, :service_date, :service_type, :cost, :notes, :owner_user_id, "
                ":odometer_reading, :next_service_date, :next_service_odometer, :vendor, "
                ":parts_cost, :labour_cost, :tax_cost, :status, :created_by, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                data,
            )
            if data["status"] == "Completed":
                conn.execute(
                    "UPDATE trucks SET last_service_date = ? WHERE truck_id = ?",
                    (data["service_date"], data["truck_id"]),
                )
            elif data["status"] == "In Progress":
                conn.execute(
                    "UPDATE trucks SET status = 'Maintenance' WHERE truck_id = ? "
                    "AND status != 'In Trip'",
                    (data["truck_id"],),
                )
            _record_user_activity(
                conn, "Maintenance Record Added",
                f"{data['service_type']} for truck {truck['registration_number']} "
                f"({data['status']}); total cost {data['cost']:.2f}."
            )
            conn.commit()
            flash("Maintenance record created.", "success")
        except ValueError as error:
            conn.rollback()
            flash(str(error), "error")
        except Exception:
            conn.rollback()
            app.logger.exception("Unable to create maintenance record.")
            flash("Could not save the maintenance record. Check the details and try again.", "error")
        finally:
            conn.close()
        return redirect(url_for("maintenance"))

    @app.route("/maintenance/<int:maintenance_id>/edit", methods=["POST"], endpoint="maintenance_edit")
    @admin_required
    def maintenance_edit(maintenance_id):
        if not require_csrf():
            return redirect(url_for("maintenance"))
        conn = get_db()
        try:
            current = scoped_record(conn, maintenance_id)
            if not current:
                abort(404)
            data = read_form()
            truck = conn.execute(
                "SELECT truck_id, registration_number, status FROM trucks WHERE truck_id = ?",
                (data["truck_id"],),
            ).fetchone()
            if not truck:
                raise ValueError("The selected truck does not exist.")
            if truck["status"] == "In Trip" and data["status"] == "In Progress":
                raise ValueError("A truck marked In Trip cannot start maintenance.")
            data["owner_user_id"] = current["owner_user_id"] or session["user_id"]
            data["created_by"] = current["created_by"] or session["user_id"]
            conn.execute(
                "UPDATE maintenance SET truck_id=:truck_id, service_date=:service_date, "
                "service_type=:service_type, cost=:cost, notes=:notes, owner_user_id=:owner_user_id, "
                "odometer_reading=:odometer_reading, next_service_date=:next_service_date, "
                "next_service_odometer=:next_service_odometer, vendor=:vendor, parts_cost=:parts_cost, "
                "labour_cost=:labour_cost, tax_cost=:tax_cost, status=:status, "
                "created_by=:created_by, updated_at=CURRENT_TIMESTAMP "
                "WHERE maintenance_id=:maintenance_id",
                {**data, "maintenance_id": maintenance_id},
            )
            for truck_id in {current["truck_id"], data["truck_id"]}:
                refresh_truck_service_state(conn, truck_id)
            _record_user_activity(
                conn, "Maintenance Record Updated",
                f"Updated maintenance record #{maintenance_id} for truck {truck['registration_number']}."
            )
            conn.commit()
            flash("Maintenance record updated.", "success")
        except ValueError as error:
            conn.rollback()
            flash(str(error), "error")
        except Exception:
            conn.rollback()
            app.logger.exception("Unable to update maintenance record.")
            flash("Could not update the maintenance record. Check the details and try again.", "error")
        finally:
            conn.close()
        return redirect(url_for("maintenance"))

    @app.route("/maintenance/<int:maintenance_id>/delete", methods=["POST"], endpoint="maintenance_delete")
    @admin_required
    def maintenance_delete(maintenance_id):
        if not require_csrf():
            return redirect(url_for("maintenance"))
        conn = get_db()
        try:
            current = scoped_record(conn, maintenance_id)
            if not current:
                abort(404)
            truck = conn.execute(
                "SELECT registration_number FROM trucks WHERE truck_id = ?",
                (current["truck_id"],),
            ).fetchone()
            conn.execute(
                "DELETE FROM maintenance WHERE maintenance_id = ?", (maintenance_id,)
            )
            refresh_truck_service_state(conn, current["truck_id"])
            _record_user_activity(
                conn, "Maintenance Record Deleted",
                f"Deleted maintenance record #{maintenance_id} for truck "
                f"{truck['registration_number'] if truck else current['truck_id']}."
            )
            conn.commit()
            flash("Maintenance record deleted.", "success")
        except Exception:
            conn.rollback()
            app.logger.exception("Unable to delete maintenance record.")
            flash("Could not delete the maintenance record.", "error")
        finally:
            conn.close()
        return redirect(url_for("maintenance"))

    # Keep all existing navigation links and url_for('maintenance') calls working.
    # The old read-only route remains registered, but its view now delegates here.
    app.view_functions["maintenance"] = list_maintenance
