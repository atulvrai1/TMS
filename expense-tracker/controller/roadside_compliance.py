"""Roadside compliance register and safety inspections for FleetFlow.

Records findings and expiry alerts; it does not make legal determinations or calculate statutory fines.
"""
import json
import os
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from flask import abort, flash, redirect, render_template, request, session, url_for

from controller.compliance_reminders import ensure_notification_schema, generate_expiry_notifications

DOCUMENT_TYPES = {
    "Driving Licence (HMV)", "Registration Certificate (RC)", "Commercial Permit",
    "National Permit", "State Permit", "Road Tax / Tax Token",
    "Fitness Certificate", "PUC Certificate", "Third-Party Insurance",
    "Trip Sheet / E-Way Bill / Cargo Manifest",
}
INSPECTION_TYPES = {
    "Documents", "Weight and Cargo", "Vehicle Safety", "Driver Behaviour", "Operational Rules",
}
FINDINGS = {
    "Overloading / weight limit concern": "Weight and Cargo",
    "Protruding cargo / missing warning flags": "Weight and Cargo",
    "Unsafe or hazardous cargo packaging": "Weight and Cargo",
    "RUPD missing / damaged": "Vehicle Safety",
    "LUPD missing / damaged": "Vehicle Safety",
    "Reflective tape missing / improper": "Vehicle Safety",
    "Lights / indicators defective": "Vehicle Safety",
    "Illegal pressure / multi-tone horn": "Vehicle Safety",
    "Number plate missing / obscured / non-compliant": "Vehicle Safety",
    "Unsafe / worn tyres": "Vehicle Safety",
    "Suspected impairment / requires authorized assessment": "Driver Behaviour",
    "No-entry / restricted-time concern": "Operational Rules",
    "Lane / signal / wrong-side / overtaking concern": "Operational Rules",
    "Mobile phone use while driving": "Driver Behaviour",
    "Seatbelt not worn": "Driver Behaviour",
    "Unauthorized passengers in goods vehicle": "Operational Rules",
    "Required document missing / expired": "Documents",
}
SEVERITIES = {"Low", "Medium", "High", "Critical"}
STATUSES = {"Open", "Corrected", "Referred", "Closed"}


def classify_expiry(expiry_value, today=None):
    """Return a UI status for a recorded ISO date; this is a reminder, not a legal ruling."""
    if not expiry_value:
        return "Expiry date missing"
    try:
        expiry = date.fromisoformat(str(expiry_value))
    except (TypeError, ValueError):
        return "Invalid expiry date"
    days = (expiry - (today or date.today())).days
    if days < 0:
        return "Expired"
    if days == 0:
        return "Due today"
    if days <= 7:
        return "Urgent"
    if days <= 30:
        return "Expiring soon"
    return "Valid"


def _ensure_compliance_schema(get_db):
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS compliance_documents (
                document_id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_type TEXT NOT NULL CHECK(subject_type IN ('truck','driver','trip')),
                truck_id INTEGER REFERENCES trucks(truck_id),
                driver_id INTEGER REFERENCES drivers(driver_id),
                trip_id INTEGER REFERENCES trips(trip_id),
                document_type TEXT NOT NULL,
                document_number TEXT,
                issue_date DATE,
                expiry_date DATE,
                verification_method TEXT NOT NULL DEFAULT 'Physical original',
                verified INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                owner_user_id INTEGER REFERENCES users(user_id),
                created_by INTEGER REFERENCES users(user_id),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS roadside_inspections (
                inspection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                truck_id INTEGER NOT NULL REFERENCES trucks(truck_id),
                driver_id INTEGER REFERENCES drivers(driver_id),
                trip_id INTEGER REFERENCES trips(trip_id),
                inspection_date DATE NOT NULL,
                location TEXT,
                officer_name TEXT,
                officer_rank TEXT,
                inspection_type TEXT NOT NULL,
                findings_json TEXT NOT NULL DEFAULT '[]',
                severity TEXT NOT NULL DEFAULT 'Low',
                status TEXT NOT NULL DEFAULT 'Open',
                challan_id INTEGER REFERENCES challans(challan_id),
                corrective_action TEXT,
                notes TEXT,
                owner_user_id INTEGER REFERENCES users(user_id),
                created_by INTEGER REFERENCES users(user_id),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_compliance_docs_expiry ON compliance_documents(expiry_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_compliance_docs_truck ON compliance_documents(truck_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_roadside_inspections_date ON roadside_inspections(inspection_date DESC)")
        ensure_notification_schema(conn)
        conn.commit()
    finally:
        conn.close()


def register_compliance_module(app, ns):
    get_db = ns["get_db"]
    admin_required = ns["admin_required"]
    _record_owner_filter = ns["_record_owner_filter"]
    _valid_csrf_token = ns["_valid_csrf_token"]
    _record_user_activity = ns["_record_user_activity"]
    _ensure_compliance_schema(get_db)

    def date_value(raw, label, required=False):
        raw = (raw or "").strip()
        if not raw:
            if required:
                raise ValueError(f"{label} is required.")
            return None
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            raise ValueError(f"{label} must be a valid date.")
        if parsed.isoformat() != raw:
            raise ValueError(f"{label} must be a valid date.")
        return raw

    def fetch_page_data(conn):
        doc_scope, doc_params = _record_owner_filter("compliance_documents")
        inspection_scope, inspection_params = _record_owner_filter("roadside_inspections")
        documents = conn.execute(
            "SELECT d.*, t.registration_number, dr.name AS driver_name, "
            "tr.trip_id AS related_trip_id, tr.route_from, tr.route_to "
            "FROM compliance_documents d LEFT JOIN trucks t ON t.truck_id=d.truck_id "
            "LEFT JOIN drivers dr ON dr.driver_id=d.driver_id "
            "LEFT JOIN trips tr ON tr.trip_id=d.trip_id "
            f"WHERE {doc_scope.replace('compliance_documents.', 'd.')} "
            "ORDER BY CASE WHEN d.expiry_date IS NOT NULL AND d.expiry_date < date('now') THEN 0 "
            "WHEN d.expiry_date IS NOT NULL AND d.expiry_date <= date('now','+30 days') THEN 1 ELSE 2 END, "
            "d.expiry_date, d.document_id DESC LIMIT 200",
            doc_params,
        ).fetchall()
        documents = [dict(row) for row in documents]
        for document in documents:
            try:
                document["days_remaining"] = (
                    date.fromisoformat(document["expiry_date"]) - date.today()
                ).days if document["expiry_date"] else None
            except (TypeError, ValueError):
                document["days_remaining"] = None
        inspection_rows = conn.execute(
            "SELECT i.*, t.registration_number, dr.name AS driver_name "
            "FROM roadside_inspections i JOIN trucks t ON t.truck_id=i.truck_id "
            "LEFT JOIN drivers dr ON dr.driver_id=i.driver_id "
            f"WHERE {inspection_scope.replace('roadside_inspections.', 'i.')} "
            "ORDER BY i.inspection_date DESC, i.inspection_id DESC LIMIT 100",
            inspection_params,
        ).fetchall()
        inspections = []
        for row in inspection_rows:
            item = dict(row)
            try:
                item["findings"] = json.loads(item.get("findings_json") or "[]")
            except (TypeError, ValueError):
                item["findings"] = []
            inspections.append(item)
        truck_scope, truck_params = _record_owner_filter("trucks")
        trucks = conn.execute(
            f"SELECT truck_id, registration_number, model FROM trucks WHERE {truck_scope} ORDER BY registration_number",
            truck_params,
        ).fetchall()
        drivers = conn.execute("SELECT driver_id, name, license_number FROM drivers ORDER BY name").fetchall()
        trips = conn.execute("SELECT trip_id, route_from, route_to, status FROM trips ORDER BY trip_id DESC LIMIT 100").fetchall()
        challan_scope, challan_params = _record_owner_filter("challans")
        try:
            challans = conn.execute(
                "SELECT challan_id, challan_number, violation FROM challans "
                f"WHERE {challan_scope} ORDER BY challan_id DESC LIMIT 100", challan_params
            ).fetchall()
        except Exception:
            challans = []
        # Advisory pre-trip check: it flags missing/expired records but does not
        # determine legal dispatch eligibility. Requirements can vary by operation/state.
        truck_document_rows = conn.execute(
            "SELECT truck_id, document_type, expiry_date FROM compliance_documents "
            f"WHERE {doc_scope} AND subject_type='truck' AND truck_id IS NOT NULL "
            "ORDER BY document_id DESC",
            doc_params,
        ).fetchall()
        latest_by_truck_type = {}
        for row in truck_document_rows:
            latest_by_truck_type.setdefault(
                (row["truck_id"], row["document_type"]), row["expiry_date"]
            )

        permit_types = ("Commercial Permit", "National Permit", "State Permit")
        today_date = date.today()
        dispatch_checks = []
        required_documents = (
            ("PUC", ("PUC Certificate",)),
            ("Insurance", ("Third-Party Insurance",)),
            ("Fitness certificate", ("Fitness Certificate",)),
        )
        for truck in trucks:
            checks = []
            for label, types in required_documents:
                recorded = any((truck["truck_id"], kind) in latest_by_truck_type for kind in types)
                expiry = next(
                    (latest_by_truck_type.get((truck["truck_id"], kind)) for kind in types
                     if (truck["truck_id"], kind) in latest_by_truck_type),
                    None,
                )
                status = "Missing record" if not recorded else classify_expiry(expiry, today_date)
                checks.append({"label": label, "status": status})

            permit_expiries = [
                latest_by_truck_type[(truck["truck_id"], kind)]
                for kind in permit_types
                if (truck["truck_id"], kind) in latest_by_truck_type
            ]
            if not permit_expiries:
                permit_status = "Missing record"
            else:
                dated_permits = [date.fromisoformat(value) for value in permit_expiries if value]
                if not dated_permits:
                    permit_status = "Expiry date missing"
                elif any(value >= today_date for value in dated_permits):
                    days = min((value - today_date).days for value in dated_permits if value >= today_date)
                    permit_status = "Due today" if days == 0 else "Urgent" if days <= 7 else "Expiring soon" if days <= 30 else "Valid"
                else:
                    permit_status = "Expired"
            checks.append({"label": "Applicable permit", "status": permit_status})
            attention = sum(1 for item in checks if item["status"] != "Valid")
            dispatch_checks.append({"truck": truck, "checks": checks, "attention_count": attention})

        return documents, inspections, trucks, drivers, trips, challans, dispatch_checks

    @app.route("/compliance", methods=["GET"], endpoint="roadside_compliance")
    @admin_required
    def roadside_compliance():
        session.setdefault("csrf_token", __import__("secrets").token_urlsafe(32))
        conn = get_db()
        try:
            documents, inspections, trucks, drivers, trips, challans, dispatch_checks = fetch_page_data(conn)
            generate_expiry_notifications(conn)
            notification_scope, notification_params = _record_owner_filter("compliance_notifications")
            notification_rows = conn.execute(
                "SELECT n.*, d.document_type, t.registration_number, dr.name AS driver_name "
                "FROM compliance_notifications n "
                "JOIN compliance_documents d ON d.document_id=n.document_id "
                "LEFT JOIN trucks t ON t.truck_id=d.truck_id "
                "LEFT JOIN drivers dr ON dr.driver_id=d.driver_id "
                f"WHERE {notification_scope.replace('compliance_notifications.', 'n.')} "
                "ORDER BY n.created_at DESC, n.notification_id DESC LIMIT 50",
                notification_params,
            ).fetchall()
            parivahan_sync_configured = bool(
                os.environ.get("PARIVAHAN_API_BASE_URL", "").strip()
                and os.environ.get("PARIVAHAN_API_TOKEN", "").strip()
            )
            today = date.today().isoformat()
            expired = sum(1 for d in documents if d["expiry_date"] and d["expiry_date"] < today)
            # Date arithmetic without relying on database timezone: due within 30 days.
            from datetime import timedelta
            expiring = sum(1 for d in documents if d["expiry_date"] and today <= d["expiry_date"] <= (date.today() + timedelta(days=30)).isoformat())
            open_findings = sum(1 for i in inspections if i["status"] == "Open")
            return render_template(
                "compliance.html", documents=documents, inspections=inspections,
                trucks=trucks, drivers=drivers, trips=trips, challans=challans,
                dispatch_checks=dispatch_checks,
                compliance_notifications=[dict(row) for row in notification_rows],
                parivahan_sync_configured=parivahan_sync_configured,
                document_types=sorted(DOCUMENT_TYPES), inspection_types=sorted(INSPECTION_TYPES),
                finding_groups={kind: [label for label, group in FINDINGS.items() if group == kind] for kind in sorted(INSPECTION_TYPES)},
                severities=sorted(SEVERITIES), statuses=sorted(STATUSES), today=today,
                expired_count=expired, expiring_count=expiring, open_findings=open_findings,
                csrf_token=session["csrf_token"], block_content="dashboard",
            )
        finally:
            conn.close()

    @app.route("/compliance/sync/<int:truck_id>", methods=["POST"], endpoint="compliance_sync_truck")
    @admin_required
    def compliance_sync_truck(truck_id):
        if not _valid_csrf_token():
            flash("Your session token is invalid. Reload the page and try again.", "error")
            return redirect(url_for("roadside_compliance"))

        base_url = os.environ.get("PARIVAHAN_API_BASE_URL", "").strip().rstrip("/")
        token = os.environ.get("PARIVAHAN_API_TOKEN", "").strip()
        parsed_url = urlsplit(base_url)
        if not base_url or not token or parsed_url.scheme != "https" or not parsed_url.netloc:
            flash(
                "Automatic sync is not configured. FleetFlow needs an authorised API endpoint and token; "
                "the public Parivahan website is not a supported API endpoint.",
                "error",
            )
            return redirect(url_for("roadside_compliance"))

        conn = get_db()
        try:
            truck_scope, truck_params = _record_owner_filter("trucks")
            truck = conn.execute(
                f"SELECT truck_id, registration_number, owner_user_id FROM trucks "
                f"WHERE truck_id=? AND {truck_scope}",
                [truck_id, *truck_params],
            ).fetchone()
            if not truck:
                abort(404)

            endpoint_url = f"{base_url}/vehicles/{quote(truck['registration_number'], safe='')}"
            api_request = Request(
                endpoint_url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                method="GET",
            )
            try:
                with urlopen(api_request, timeout=12) as response:
                    if response.status != 200:
                        raise ValueError("The authorised vehicle-data provider returned an unexpected status.")
                    payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
                app.logger.warning("Compliance sync failed for truck %s (%s)", truck_id, type(error).__name__)
                flash("The authorised vehicle-data service could not be reached. No records were changed.", "error")
                return redirect(url_for("roadside_compliance"))

            if not isinstance(payload, dict) or not isinstance(payload.get("documents"), list):
                flash("The API response did not match the documented FleetFlow vehicle-data format. No records were changed.", "error")
                return redirect(url_for("roadside_compliance"))

            source = str(payload.get("source", "")).strip().casefold()
            synced = 0
            for item in payload["documents"]:
                if not isinstance(item, dict):
                    continue
                doc_type = str(item.get("document_type", "")).strip()
                if doc_type not in DOCUMENT_TYPES or doc_type in {
                    "Driving Licence (HMV)", "Trip Sheet / E-Way Bill / Cargo Manifest"
                }:
                    continue
                number = str(item.get("document_number", "")).strip()[:120]
                issue = date_value(item.get("issue_date"), "Issue date")
                expiry = date_value(item.get("expiry_date"), "Expiry date")
                if not expiry or (issue and expiry < issue):
                    continue
                # Only a response explicitly identifying Parivahan as the source can
                # be marked source-verified; all other imports remain unverified.
                verified = 1 if source == "parivahan" and item.get("verified") is True else 0
                notes = f"Imported from authorised API; source={source or 'unspecified'}."[:500]
                existing = None
                if number:
                    existing = conn.execute(
                        "SELECT document_id FROM compliance_documents "
                        "WHERE truck_id=? AND document_type=? AND document_number=? "
                        "ORDER BY document_id DESC LIMIT 1",
                        (truck_id, doc_type, number),
                    ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE compliance_documents SET issue_date=?, expiry_date=?, "
                        "verification_method='Authorised API sync', verified=?, notes=?, "
                        "updated_at=CURRENT_TIMESTAMP WHERE document_id=?",
                        (issue, expiry, verified, notes, existing["document_id"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO compliance_documents "
                        "(subject_type, truck_id, document_type, document_number, issue_date, expiry_date, "
                        "verification_method, verified, notes, owner_user_id, created_by, created_at) "
                        "VALUES ('truck', ?, ?, ?, ?, ?, 'Authorised API sync', ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                        (truck_id, doc_type, number or None, issue, expiry, verified, notes,
                         truck["owner_user_id"], session["user_id"]),
                    )
                synced += 1
            if synced:
                generate_expiry_notifications(conn)
                _record_user_activity(conn, "Compliance API Sync", f"Synced {synced} document record(s) for truck {truck['registration_number']}.")
                conn.commit()
                flash(f"Imported or updated {synced} document record(s) for {truck['registration_number']}. Review the source and expiry dates.", "success")
            else:
                conn.rollback()
                flash("The provider returned no supported document records. Existing records were not changed.", "info")
        except ValueError as error:
            conn.rollback()
            flash(str(error), "error")
        except Exception:
            conn.rollback()
            app.logger.exception("Unable to sync compliance documents")
            flash("Compliance sync failed safely. Existing records were not changed.", "error")
        finally:
            conn.close()
        return redirect(url_for("roadside_compliance"))

    @app.route("/compliance/documents/add", methods=["POST"], endpoint="compliance_document_add")
    @admin_required
    def compliance_document_add():
        if not _valid_csrf_token():
            flash("Your session token is invalid. Reload the page and try again.", "error")
            return redirect(url_for("roadside_compliance"))
        conn = get_db()
        try:
            subject = request.form.get("subject_type", "").strip()
            doc_type = request.form.get("document_type", "").strip()
            truck_id = request.form.get("truck_id", "").strip()
            driver_id = request.form.get("driver_id", "").strip()
            number = request.form.get("document_number", "").strip()[:120]
            issue = date_value(request.form.get("issue_date"), "Issue date")
            expiry = date_value(request.form.get("expiry_date"), "Expiry date")
            method = request.form.get("verification_method", "Physical original").strip()
            verified = 1 if request.form.get("verified") == "1" else 0
            notes = request.form.get("notes", "").strip()[:500]
            if subject not in {"truck", "driver", "trip"}:
                raise ValueError("Choose whether this document belongs to a truck or driver.")
            if doc_type not in DOCUMENT_TYPES:
                raise ValueError("Choose a supported document type.")
            if method not in {"Physical original", "DigiLocker", "mParivahan", "Other verified digital copy", "Not verified"}:
                raise ValueError("Choose a valid verification method.")
            if expiry and issue and expiry < issue:
                raise ValueError("Expiry date cannot be earlier than issue date.")
            trip_id = request.form.get("trip_id", "").strip()
            if subject == "truck":
                if not truck_id.isdigit() or not conn.execute("SELECT 1 FROM trucks WHERE truck_id=?", (int(truck_id),)).fetchone():
                    raise ValueError("Select a valid truck.")
                if doc_type in {"Driving Licence (HMV)", "Trip Sheet / E-Way Bill / Cargo Manifest"}:
                    raise ValueError("Select the driver for a licence or the trip for a trip document.")
                truck_id_value, driver_id_value, trip_id_value = int(truck_id), None, None
            elif subject == "driver":
                if not driver_id.isdigit() or not conn.execute("SELECT 1 FROM drivers WHERE driver_id=?", (int(driver_id),)).fetchone():
                    raise ValueError("Select a valid driver.")
                if doc_type != "Driving Licence (HMV)":
                    raise ValueError("Only the Driving Licence belongs to a driver.")
                truck_id_value, driver_id_value, trip_id_value = None, int(driver_id), None
            else:
                if not trip_id.isdigit() or not conn.execute("SELECT 1 FROM trips WHERE trip_id=?", (int(trip_id),)).fetchone():
                    raise ValueError("Select a valid trip.")
                if doc_type != "Trip Sheet / E-Way Bill / Cargo Manifest":
                    raise ValueError("Select Trip Sheet / E-Way Bill / Cargo Manifest for trip records.")
                truck_id_value, driver_id_value, trip_id_value = None, None, int(trip_id)
            conn.execute(
                "INSERT INTO compliance_documents (subject_type,truck_id,driver_id,trip_id,document_type,document_number,"
                "issue_date,expiry_date,verification_method,verified,notes,owner_user_id,created_by,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (subject, truck_id_value, driver_id_value, trip_id_value, doc_type, number or None, issue, expiry, method, verified,
                 notes or None, session["user_id"], session["user_id"]),
            )
            _record_user_activity(conn, "Compliance Document Added", f"{doc_type} added to {subject} record.")
            conn.commit()
            flash("Compliance document saved.", "success")
        except ValueError as error:
            conn.rollback()
            flash(str(error), "error")
        except Exception:
            conn.rollback()
            app.logger.exception("Unable to save compliance document")
            flash("Could not save the document. Please check the values and try again.", "error")
        finally:
            conn.close()
        return redirect(url_for("roadside_compliance"))

    @app.route("/compliance/inspections/add", methods=["POST"], endpoint="compliance_inspection_add")
    @admin_required
    def compliance_inspection_add():
        if not _valid_csrf_token():
            flash("Your session token is invalid. Reload the page and try again.", "error")
            return redirect(url_for("roadside_compliance"))
        conn = get_db()
        try:
            truck_id = request.form.get("truck_id", "").strip()
            driver_id = request.form.get("driver_id", "").strip()
            trip_id = request.form.get("trip_id", "").strip()
            challan_id = request.form.get("challan_id", "").strip()
            inspection_date = date_value(request.form.get("inspection_date"), "Inspection date", True)
            kind = request.form.get("inspection_type", "").strip()
            severity = request.form.get("severity", "Low").strip()
            status = request.form.get("status", "Open").strip()
            location = request.form.get("location", "").strip()[:160]
            officer = request.form.get("officer_name", "").strip()[:120]
            rank = request.form.get("officer_rank", "").strip()[:120]
            action = request.form.get("corrective_action", "").strip()[:1000]
            notes = request.form.get("notes", "").strip()[:1000]
            findings = request.form.getlist("findings")
            if not truck_id.isdigit() or not conn.execute("SELECT 1 FROM trucks WHERE truck_id=?", (int(truck_id),)).fetchone():
                raise ValueError("Select a valid truck for the inspection.")
            if driver_id and (not driver_id.isdigit() or not conn.execute("SELECT 1 FROM drivers WHERE driver_id=?", (int(driver_id),)).fetchone()):
                raise ValueError("Select a valid driver.")
            if trip_id and (not trip_id.isdigit() or not conn.execute("SELECT 1 FROM trips WHERE trip_id=?", (int(trip_id),)).fetchone()):
                raise ValueError("Select a valid trip.")
            if challan_id and (not challan_id.isdigit() or not conn.execute("SELECT 1 FROM challans WHERE challan_id=?", (int(challan_id),)).fetchone()):
                raise ValueError("Select a valid linked challan.")
            if kind not in INSPECTION_TYPES or severity not in SEVERITIES or status not in STATUSES:
                raise ValueError("Choose valid inspection type, severity, and status.")
            invalid_findings = [f for f in findings if f not in FINDINGS or FINDINGS[f] != kind]
            if invalid_findings:
                raise ValueError("The selected findings do not match the inspection type.")
            if len(set(findings)) != len(findings) or len(findings) > 20:
                raise ValueError("Duplicate or too many findings selected.")
            if len(location) > 160 or len(officer) > 120 or len(rank) > 120:
                raise ValueError("Officer or location details are too long.")
            conn.execute(
                "INSERT INTO roadside_inspections (truck_id,driver_id,trip_id,inspection_date,location,officer_name,"
                "officer_rank,inspection_type,findings_json,severity,status,challan_id,corrective_action,notes,"
                "owner_user_id,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (int(truck_id), int(driver_id) if driver_id else None, int(trip_id) if trip_id else None,
                 inspection_date, location or None, officer or None, rank or None, kind, json.dumps(findings),
                 severity, status, int(challan_id) if challan_id else None, action or None, notes or None,
                 session["user_id"], session["user_id"]),
            )
            _record_user_activity(conn, "Roadside Inspection Recorded",
                                  f"{kind} inspection recorded for truck ID {truck_id}; severity {severity}; findings {len(findings)}.")
            conn.commit()
            flash("Roadside inspection saved.", "success")
        except ValueError as error:
            conn.rollback()
            flash(str(error), "error")
        except Exception:
            conn.rollback()
            app.logger.exception("Unable to save roadside inspection")
            flash("Could not save the inspection. Please check the values and try again.", "error")
        finally:
            conn.close()
        return redirect(url_for("roadside_compliance"))

    @app.route("/compliance/inspections/<int:inspection_id>/update", methods=["POST"], endpoint="compliance_inspection_update")
    @admin_required
    def compliance_inspection_update(inspection_id):
        if not _valid_csrf_token():
            flash("Your session token is invalid. Reload the page and try again.", "error")
            return redirect(url_for("roadside_compliance"))
        conn = get_db()
        try:
            scope, params = _record_owner_filter("roadside_inspections")
            row = conn.execute(
                "SELECT inspection_id FROM roadside_inspections WHERE inspection_id=? AND " + scope,
                [inspection_id, *params],
            ).fetchone()
            if not row:
                abort(404)
            status = request.form.get("status", "").strip()
            action = request.form.get("corrective_action", "").strip()[:1000]
            if status not in STATUSES:
                raise ValueError("Choose a valid status.")
            conn.execute("UPDATE roadside_inspections SET status=?, corrective_action=?, updated_at=CURRENT_TIMESTAMP WHERE inspection_id=?",
                         (status, action or None, inspection_id))
            _record_user_activity(conn, "Roadside Inspection Updated", f"Inspection #{inspection_id} status changed to {status}.")
            conn.commit()
            flash("Inspection follow-up updated.", "success")
        except ValueError as error:
            conn.rollback()
            flash(str(error), "error")
        except Exception:
            conn.rollback()
            app.logger.exception("Unable to update inspection follow-up")
            flash("Could not update the inspection.", "error")
        finally:
            conn.close()
        return redirect(url_for("roadside_compliance"))
