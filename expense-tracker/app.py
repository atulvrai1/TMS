import os
import csv
import io
import hashlib
import re
import secrets
import sqlite3
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

load_dotenv(Path(__file__).resolve().parent / ".env")

from database.db import get_db, init_db, log_activity
from controller.notification_service import NotificationService
from controller.access_controller import (
    SUPER_ADMIN_EMAIL,
    UserAccessController,
    access_required,
    admin_required
)

app = Flask(__name__)
IS_PRODUCTION = os.environ.get("APP_ENV", "").strip().lower() == "production"
app.config.update(
    PUBLIC_BASE_URL=os.environ.get("FLEETFLOW_PUBLIC_URL", "").rstrip("/"),
    MAX_CONTENT_LENGTH=11 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SECURE=IS_PRODUCTION,
    PERMANENT_SESSION_LIFETIME=__import__("datetime").timedelta(hours=8),
)
DRIVER_DOCUMENT_MAX_SIZE = 10 * 1024 * 1024
DRIVER_DOCUMENT_TYPES = {
    "Driving license": {".pdf", ".jpg", ".jpeg", ".png"},
    "Government ID": {".pdf", ".jpg", ".jpeg", ".png"},
    "Aadhaar card": {".pdf", ".jpg", ".jpeg", ".png"},
    "Tax document": {".pdf", ".jpg", ".jpeg", ".png"},
    "Address proof": {".pdf", ".jpg", ".jpeg", ".png"},
    "Other": {".pdf", ".jpg", ".jpeg", ".png"},
}
DRIVER_DOCUMENT_SIGNATURES = {
    ".pdf": (b"%PDF-",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
}


def _get_secret_key():
    configured_key = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if configured_key:
        if len(configured_key) < 32:
            raise RuntimeError("FLASK_SECRET_KEY must contain at least 32 characters.")
        return configured_key
    if IS_PRODUCTION:
        raise RuntimeError(
            "FLASK_SECRET_KEY must be configured in production; generate one with "
            "'python -c \"import secrets; print(secrets.token_hex(32))\"'."
        )

    # Development only: create an ignored, permission-restricted key file.
    key_path = Path(__file__).resolve().parent / ".flask_secret_key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = key_path.read_text(encoding="utf-8").strip()
        if len(key) >= 32:
            return key
        raise RuntimeError(f"Development Flask secret key file is invalid: {key_path}")

    key = secrets.token_hex(32)
    with os.fdopen(descriptor, "w", encoding="utf-8") as key_file:
        key_file.write(key)
        key_file.flush()
        os.fsync(key_file.fileno())
    return key


app.config["SECRET_KEY"] = _get_secret_key()


@app.get("/healthz")
def healthz():
    """Container/orchestrator health probe; verify the app can reach its database."""
    conn = None
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        return jsonify(status="ok"), 200
    except Exception:
        app.logger.exception("FleetFlow health check failed")
        return jsonify(status="unhealthy"), 503
    finally:
        if conn is not None:
            conn.close()



def _password_matches(stored_password, candidate):
    """Accept only Werkzeug password hashes; never authenticate plaintext secrets."""
    if not candidate or not stored_password:
        return False
    if not stored_password.startswith(("scrypt:", "pbkdf2:")):
        return False
    try:
        return check_password_hash(stored_password, candidate)
    except (ValueError, TypeError):
        return False


def _avatar_initials(full_name, email):
    name_parts = (full_name or "").split()
    if name_parts:
        return "".join(part[0] for part in name_parts[:2]).upper()
    email_name = (email or "").split("@", 1)[0]
    return email_name[:2].upper() or "A"


def _record_user_activity(conn, action, details, owner_user_id=None):
    conn.execute(
        "INSERT INTO user_logs (user_id, action, details, actor_user_id) "
        "VALUES (?, ?, ?, ?)",
        (owner_user_id or session["user_id"], action, details, session["user_id"])
    )


def _is_admin_user():
    return bool(session.get("is_admin") or session.get("role") == "Admin")


def _record_owner_filter(alias, owner_column="owner_user_id"):
    if session.get("email", "").lower() == SUPER_ADMIN_EMAIL.lower():
        return "1 = 1", []
    if _is_admin_user():
        # Admins can review records across the FleetFlow workspace, including
        # records created by other administrators. Non-admin users remain scoped.
        return "1 = 1", []
    if alias in ("trips", "t"):
        # Drivers should access trips assigned to their linked driver record,
        # even when an administrator owns/created the trip. Support the common
        # short alias used by analytics queries as well as the table name.
        return (
            f"{alias}.driver_id = (SELECT driver_id FROM users WHERE user_id = ?)",
            [session["user_id"]]
        )
    return f"{alias}.{owner_column} = ?", [session["user_id"]]


def _load_assignable_users(conn):
    if session.get("email", "").lower() == SUPER_ADMIN_EMAIL.lower():
        role_filter = ""
        params = []
    else:
        role_filter = "AND (role = 'User' OR user_id = ?) "
        params = [session["user_id"]]
    return conn.execute(
        "SELECT user_id, full_name, email, role, driver_id FROM users "
        "WHERE is_approved = 1 AND is_rejected = 0 AND is_terminated = 0 "
        + role_filter
        + "ORDER BY full_name COLLATE NOCASE, email COLLATE NOCASE",
        params
    ).fetchall()


def _is_assignable_owner(conn, owner_user_id):
    owner_scope = ""
    params = [owner_user_id]
    if session.get("email", "").lower() != SUPER_ADMIN_EMAIL.lower():
        owner_scope = "AND (role = 'User' OR user_id = ?) "
        params.append(session["user_id"])
    return conn.execute(
        "SELECT 1 FROM users WHERE user_id = ? AND is_approved = 1 "
        "AND is_rejected = 0 AND is_terminated = 0 "
        + owner_scope,
        params
    ).fetchone() is not None


def _store_driver_document(file_storage):
    original_name = secure_filename(file_storage.filename or "")
    suffix = Path(original_name).suffix.lower()
    if not original_name or suffix not in {".pdf", ".jpg", ".jpeg", ".png"}:
        raise ValueError("Choose a PDF, JPG, JPEG, or PNG document.")

    content = file_storage.stream.read(DRIVER_DOCUMENT_MAX_SIZE + 1)
    if not content:
        raise ValueError("The selected document is empty.")
    if len(content) > DRIVER_DOCUMENT_MAX_SIZE:
        raise ValueError("Each driver document must be 10 MB or smaller.")
    if not any(content.startswith(signature) for signature in DRIVER_DOCUMENT_SIGNATURES[suffix]):
        raise ValueError("The selected file does not match its file type.")

    storage_name = f"{secrets.token_hex(24)}{suffix}"
    storage_dir = Path(app.instance_path) / "driver_documents"
    storage_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(storage_dir, 0o700)
    file_path = storage_dir / storage_name
    descriptor = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stored_file:
            stored_file.write(content)
            stored_file.flush()
            os.fsync(stored_file.fileno())
    except Exception:
        file_path.unlink(missing_ok=True)
        raise
    return original_name, storage_name, len(content)


def _issue_password_reset(conn, user, requested_by):
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE password_reset_tokens SET used_at = CURRENT_TIMESTAMP, admin_reset_url = NULL "
        "WHERE user_id = ? AND used_at IS NULL",
        (user["user_id"],)
    )
    conn.execute(
        "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at, requested_by) "
        "VALUES (?, ?, datetime('now', '+30 minutes'), ?)",
        (user["user_id"], token_hash, requested_by)
    )
    action = (
        "Admin Password Reset Requested"
        if requested_by is not None
        else "Password Reset Requested"
    )
    details = (
        f"Administrator requested a password reset link for {user['email']}."
        if requested_by is not None
        else "Requested a password reset link."
    )
    conn.execute(
        "INSERT INTO user_logs (user_id, action, details, actor_user_id) "
        "VALUES (?, ?, ?, ?)",
        (user["user_id"], action, details, requested_by)
    )
    conn.commit()

    reset_path = url_for("reset_password", token=token)
    reset_url = (
        f"{app.config['PUBLIC_BASE_URL']}{reset_path}"
        if app.config["PUBLIC_BASE_URL"]
        else request.url_root.rstrip("/") + reset_path
    )
    try:
        delivered_by_email = NotificationService.send_password_reset_email(user["email"], reset_url)
    except Exception:
        conn.execute(
            "UPDATE password_reset_tokens SET used_at = CURRENT_TIMESTAMP "
            "WHERE token_hash = ? AND used_at IS NULL",
            (token_hash,)
        )
        conn.commit()
        raise
    delivery_status = "email" if delivered_by_email else "admin_queue"
    conn.execute(
        "UPDATE password_reset_tokens SET delivery_status = ?, "
        "admin_reset_url = CASE "
        "WHEN ? = 'admin_queue' AND ? IS NULL AND EXISTS "
        "(SELECT 1 FROM users WHERE users.user_id = password_reset_tokens.user_id "
        "AND users.is_approved = 1 AND users.is_rejected = 0 "
        "AND users.is_terminated = 0) THEN ? ELSE NULL END "
        "WHERE token_hash = ?",
        (delivery_status, delivery_status, requested_by, reset_url, token_hash)
    )
    conn.commit()
    return token_hash, delivered_by_email, reset_url


# Initialize database on startup
with app.app_context():
    init_db()


# ------------------------------------------------------------------ #
# Common Routes                                                     #
# ------------------------------------------------------------------ #

@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    login_role = request.form.get("login_role", "user") if request.method == "POST" else request.args.get("role", "user")
    login_role = login_role.lower()
    if login_role not in ("user", "admin"):
        login_role = "user"

    if request.method == "POST":
        if not _valid_csrf_token():
            return render_template(
                "login.html",
                error="Your session token is invalid. Reload the page and try again.",
                login_role=login_role,
                csrf_token=session["csrf_token"]
            )
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

        if user and _password_matches(user['password'], password):
            if not user['password'].startswith(("scrypt:", "pbkdf2:")):
                conn.execute(
                    "UPDATE users SET password = ? WHERE user_id = ?",
                    (generate_password_hash(password), user['user_id'])
                )
                conn.commit()
            if user['is_rejected']:
                conn.close()
                return render_template(
                    "login.html",
                    error="Your registration was rejected. Please contact the administrator.",
                    login_role=login_role,
                    csrf_token=session["csrf_token"]
                )
            if user['is_terminated']:
                conn.close()
                return render_template(
                    "login.html",
                    error="This account has been terminated. Contact an administrator if you believe this is a mistake.",
                    login_role=login_role,
                    csrf_token=session["csrf_token"]
                )
            is_admin = (
                user['role'] == 'Admin'
                or UserAccessController(conn).is_super_admin(user['email'])
            )
            if (login_role == "admin") != bool(is_admin):
                conn.close()
                return render_template(
                    "login.html",
                    error=f"These credentials are not valid for {login_role} login.",
                    login_role=login_role,
                    csrf_token=session["csrf_token"]
                )
            # Rotate the signed session on successful authentication.
            session.clear()
            session['csrf_token'] = secrets.token_urlsafe(32)
            session['user_id'] = user['user_id']
            session['email'] = user['email']
            session['role'] = user['role']
            session['job_role'] = user['job_role']
            session['driver_id'] = user['driver_id']
            session['full_name'] = user['full_name']
            session['avatar_initials'] = _avatar_initials(user['full_name'], user['email'])
            session['is_admin'] = bool(is_admin)
            _record_user_activity(conn, "Account Login", "Signed in successfully.")
            conn.commit()
            conn.close()
            flash("Welcome back to FleetFlow!", "success")
            return redirect(url_for("admin_dashboard" if is_admin else "trips"))

        conn.close()
        return render_template(
            "login.html",
            error="Invalid email or password",
            login_role=login_role,
            csrf_token=session["csrf_token"]
        )

    return render_template(
        "login.html",
        login_role=login_role,
        csrf_token=session["csrf_token"]
    )


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    error = None
    requested = False
    recovery_link = None
    if request.method == "POST":
        if not _valid_csrf_token():
            error = "Your session token is invalid. Reload the page and try again."
        else:
            email = request.form.get("email", "").strip().lower()
            conn = get_db()
            user = conn.execute(
                "SELECT user_id, email FROM users "
                "WHERE lower(email) = ? AND is_approved = 1 "
                "AND is_rejected = 0 AND is_terminated = 0",
                (email,)
            ).fetchone() if email else None
            try:
                if user:
                    _, delivered_by_email, reset_url = _issue_password_reset(conn, user, None)
                    # Never expose a reset token to the browser in production.
                    # In development, provide the generated link directly if SMTP is unavailable.
                    if not delivered_by_email and not IS_PRODUCTION:
                        recovery_link = reset_url
                requested = True
            except Exception:
                app.logger.exception("Failed to issue a self-service password reset.")
                error = "Password recovery is temporarily unavailable. Please contact an administrator."
            finally:
                conn.close()
    return render_template(
        "forgot_password.html",
        csrf_token=session["csrf_token"],
        error=error,
        requested=requested,
        recovery_link=recovery_link
    )


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = get_db()
    reset = conn.execute(
        "SELECT password_reset_tokens.user_id, users.role "
        "FROM password_reset_tokens "
        "JOIN users ON users.user_id = password_reset_tokens.user_id "
        "WHERE password_reset_tokens.token_hash = ? "
        "AND password_reset_tokens.used_at IS NULL "
        "AND password_reset_tokens.expires_at > CURRENT_TIMESTAMP "
        "AND users.is_approved = 1 AND users.is_rejected = 0 "
        "AND users.is_terminated = 0",
        (token_hash,)
    ).fetchone()
    conn.close()
    if not reset:
        return render_template(
            "reset_password.html",
            invalid=True,
            csrf_token=session["csrf_token"]
        ), 400

    error = None
    if request.method == "POST":
        if not _valid_csrf_token():
            error = "Your session token is invalid. Reload this page and try again."
        else:
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            if len(new_password) < 8:
                error = "Password must be at least 8 characters."
            elif len(new_password) > 256:
                error = "Password must be 256 characters or fewer."
            elif new_password != confirm_password:
                error = "Password and confirmation do not match."

            if not error:
                conn = get_db()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    current_reset = conn.execute(
                        "SELECT password_reset_tokens.user_id FROM password_reset_tokens "
                        "JOIN users ON users.user_id = password_reset_tokens.user_id "
                        "WHERE token_hash = ? AND used_at IS NULL "
                        "AND expires_at > CURRENT_TIMESTAMP "
                        "AND users.is_approved = 1 AND users.is_rejected = 0 "
                        "AND users.is_terminated = 0",
                        (token_hash,)
                    ).fetchone()
                    if not current_reset:
                        conn.rollback()
                        error = "This password reset link has expired or has already been used."
                    else:
                        conn.execute(
                            "UPDATE users SET password = ? WHERE user_id = ?",
                            (generate_password_hash(new_password), current_reset["user_id"])
                        )
                        conn.execute(
                            "UPDATE password_reset_tokens SET used_at = CURRENT_TIMESTAMP "
                            ", admin_reset_url = NULL WHERE token_hash = ?",
                            (token_hash,)
                        )
                        conn.execute(
                            "UPDATE password_reset_tokens SET used_at = CURRENT_TIMESTAMP "
                            ", admin_reset_url = NULL WHERE user_id = ? AND used_at IS NULL",
                            (current_reset["user_id"],)
                        )
                        conn.execute(
                            "INSERT INTO user_logs (user_id, action, details) "
                            "VALUES (?, 'Password Reset Completed', 'Reset password using a one-time recovery link.')",
                            (current_reset["user_id"],)
                        )
                        conn.commit()
                        login_role = "admin" if reset["role"] == "Admin" else "user"
                        conn.close()
                        flash("Your password has been reset. Sign in with your new password.", "success")
                        return redirect(url_for("login", role=login_role))
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    if conn:
                        conn.close()

    return render_template(
        "reset_password.html",
        invalid=False,
        csrf_token=session["csrf_token"],
        error=error
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    requested_role = "User"
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password")
        requested_role = request.form.get("requested_role", "User")

        if not _valid_csrf_token():
            return render_template(
                "register.html",
                error="Your session token is invalid. Reload the page and try again.",
                csrf_token=session["csrf_token"],
                requested_role=requested_role
            )

        if not full_name or not email or not password:
            return render_template(
                "register.html",
                error="Full name, email, and password are required",
                csrf_token=session["csrf_token"],
                requested_role=requested_role
            )
        if (
            len(full_name) > 120
            or len(email) > 254
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
        ):
            return render_template(
                "register.html",
                error="Enter a valid name and email address.",
                csrf_token=session["csrf_token"],
                requested_role=requested_role
            )
        if len(password) < 8:
            return render_template(
                "register.html",
                error="Password must be at least 8 characters.",
                csrf_token=session["csrf_token"],
                requested_role=requested_role
            )
        if len(password) > 128:
            return render_template(
                "register.html",
                error="Password must be 128 characters or fewer.",
                csrf_token=session["csrf_token"],
                requested_role=requested_role
            )
        if requested_role not in ("User", "Admin"):
            return render_template(
                "register.html",
                error="Select a valid account access level.",
                csrf_token=session["csrf_token"],
                requested_role="User"
            )

        conn = get_db()
        try:
            cursor = conn.execute(
                "INSERT INTO users (full_name, email, password, is_approved, requested_role) "
                "VALUES (?, ?, ?, 0, ?)",
                (full_name, email, generate_password_hash(password), requested_role)
            )
            user_id = cursor.lastrowid
            conn.commit()
            log_activity(
                user_id,
                "Account Registered",
                f"User {email} registered requesting {requested_role} access."
            )
        except Exception:
            app.logger.exception("Account registration failed.")
            conn.close()
            return render_template(
                "register.html",
                error="Registration failed. Email might already exist.",
                csrf_token=session["csrf_token"],
                requested_role=requested_role
            )
        finally:
            conn.close()

        return render_template(
            "register.html",
            success="Registration submitted. An administrator must approve your requested access before you can sign in.",
            csrf_token=session["csrf_token"],
            requested_role=requested_role
        )

    return render_template(
        "register.html",
        csrf_token=session["csrf_token"],
        requested_role=requested_role
    )


@app.route("/logout", methods=["POST"])
def logout():
    if not _valid_csrf_token():
        flash("Your session token is invalid. Refresh the page and try again.", "error")
        return redirect(url_for("account"))
    if session.get('user_id') is not None:
        conn = get_db()
        _record_user_activity(conn, "Account Logout", "Signed out.")
        conn.commit()
        conn.close()
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("landing"))


@app.route("/pending-approval")
def pending_approval():
    rejected = False
    if session.get('user_id') is not None:
        conn = get_db()
        user = conn.execute(
            "SELECT is_rejected FROM users WHERE user_id = ?",
            (session['user_id'],)
        ).fetchone()
        conn.close()
        rejected = bool(user and user['is_rejected'])
    return render_template("pending_approval.html", rejected=rejected)


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


# ------------------------------------------------------------------ #
# Fleet Management Module                                           #
# ------------------------------------------------------------------ #

@app.route("/trucks")
@access_required
def trucks():
    if not _is_admin_user():
        abort(403)
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    is_admin = _is_admin_user()
    conn = get_db()
    trucks = conn.execute(
        "SELECT * FROM trucks ORDER BY registration_number"
    ).fetchall()

    total_trucks = len(trucks)
    available_trucks = len([t for t in trucks if t['status'] == 'Available'])
    in_trip_trucks = len([t for t in trucks if t['status'] == 'In Trip'])
    maint_trucks = len([t for t in trucks if t['status'] == 'Maintenance'])

    conn.close()
    return render_template("trucks.html",
                           trucks=trucks,
                           total_trucks=total_trucks,
                           available_trucks=available_trucks,
                           in_trip_trucks=in_trip_trucks,
                           maint_trucks=maint_trucks,
                           is_admin=is_admin,
                           block_content='dashboard')


@app.route("/drivers")
@access_required
def drivers():
    if not _is_admin_user():
        abort(403)
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    is_admin = _is_admin_user()
    conn = get_db()
    if is_admin:
        rows = conn.execute(
            "SELECT drivers.driver_id, drivers.name, drivers.license_number, "
            "drivers.contact_info, drivers.status, drivers.is_terminated, "
            "drivers.terminated_at, drivers.termination_reason "
            "FROM drivers ORDER BY name COLLATE NOCASE"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT driver_id, name, status FROM drivers "
            "WHERE is_terminated = 0 ORDER BY name COLLATE NOCASE"
        ).fetchall()
    counts = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status = 'Active' AND is_terminated = 0 THEN 1 ELSE 0 END) AS active, "
        "SUM(CASE WHEN status = 'On Leave' AND is_terminated = 0 THEN 1 ELSE 0 END) AS on_leave, "
        "SUM(CASE WHEN is_terminated = 1 THEN 1 ELSE 0 END) AS terminated "
        "FROM drivers"
    ).fetchone()
    conn.close()
    return render_template(
        "drivers.html",
        drivers=rows,
        counts=counts,
        is_admin=is_admin,
        csrf_token=session["csrf_token"],
        block_content='dashboard'
    )


@app.route("/drivers/add", methods=["GET", "POST"])
@admin_required
def add_driver():
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    if request.method == "POST":
        if not _valid_csrf_token():
            flash("Your session token is invalid. Reload the page and try again.", "error")
            return redirect(url_for("drivers"))

        name = request.form.get("name", "").strip()
        license_number = request.form.get("license_number", "").strip()
        contact_info = request.form.get("contact_info", "").strip()
        status = request.form.get("status", "Active")
        if not name or len(name) > 120 or not license_number or len(license_number) > 80:
            flash("Enter a driver name and license number within the allowed lengths.", "error")
            return redirect(url_for("add_driver"))
        if len(contact_info) > 200 or status not in {"Active", "On Leave"}:
            flash("Enter valid contact details and driver status.", "error")
            return redirect(url_for("add_driver"))

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO drivers (name, license_number, contact_info, status) "
                "VALUES (?, ?, ?, ?)",
                (name, license_number, contact_info or None, status)
            )
            _record_user_activity(
                conn,
                "Driver Added",
                f"Added driver {name}."
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            flash("That license number is already registered.", "error")
            return redirect(url_for("add_driver"))
        finally:
            conn.close()
        flash(f"Driver {name} added successfully.", "success")
        return redirect(url_for("drivers"))

    conn = get_db()
    conn.close()
    return render_template(
        "driver_form.html",
        driver=None,
        csrf_token=session["csrf_token"],
        block_content="dashboard"
    )


@app.route("/drivers/<int:id>/edit", methods=["GET", "POST"])
@admin_required
def edit_driver(id):
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    conn = get_db()
    driver = conn.execute(
        "SELECT driver_id, name, license_number, contact_info, status, "
        "is_terminated, terminated_at, termination_reason "
        "FROM drivers WHERE driver_id = ?",
        (id,)
    ).fetchone()
    if not driver:
        conn.close()
        return "Driver not found.", 404
    documents = conn.execute(
        "SELECT document_id, document_type, original_name, file_size, uploaded_at "
        "FROM driver_documents WHERE driver_id = ? "
        "ORDER BY uploaded_at DESC, document_id DESC",
        (id,)
    ).fetchall()

    if request.method == "POST":
        if not _valid_csrf_token():
            conn.close()
            flash("Your session token is invalid. Reload the page and try again.", "error")
            return redirect(url_for("edit_driver", id=id))

        name = request.form.get("name", "").strip()
        license_number = request.form.get("license_number", "").strip()
        contact_info = request.form.get("contact_info", "").strip()
        status = request.form.get("status", "")
        termination_reason = request.form.get("termination_reason", "").strip()
        if not name or len(name) > 120 or not license_number or len(license_number) > 80:
            conn.close()
            flash("Enter a driver name and license number within the allowed lengths.", "error")
            return redirect(url_for("edit_driver", id=id))
        if len(contact_info) > 200 or status not in {"Active", "On Leave", "Departed"}:
            conn.close()
            flash("Enter valid contact details and driver status.", "error")
            return redirect(url_for("edit_driver", id=id))
        if status == "Departed" and not 5 <= len(termination_reason) <= 300:
            conn.close()
            flash("Enter a departure reason in 5 to 300 characters.", "error")
            return redirect(url_for("edit_driver", id=id))
        try:
            conn.execute("BEGIN IMMEDIATE")
            if status in {"On Leave", "Departed"}:
                active_trip = conn.execute(
                    "SELECT trip_id FROM trips WHERE driver_id = ? "
                    "AND status IN ('Scheduled', 'In Progress') LIMIT 1",
                    (id,)
                ).fetchone()
                if active_trip:
                    conn.rollback()
                    flash(
                        f"A driver assigned to scheduled or in-progress Trip "
                        f"#{active_trip['trip_id']} cannot be marked {status.lower()}.",
                        "error"
                    )
                    return redirect(url_for("edit_driver", id=id))

            was_terminated = bool(driver["is_terminated"])
            is_terminated = status == "Departed"
            if is_terminated:
                departure_reason = termination_reason
                conn.execute(
                    "UPDATE drivers SET name = ?, license_number = ?, contact_info = ?, "
                    "is_terminated = 1, terminated_at = COALESCE(terminated_at, CURRENT_TIMESTAMP), "
                    "termination_reason = ? WHERE driver_id = ?",
                    (name, license_number, contact_info or None, departure_reason, id)
                )
                action = "Driver Marked Departed" if not was_terminated else "Driver Updated"
                details = (
                    f"Marked driver {name} as departed. Reason: {departure_reason}"
                    if not was_terminated else f"Updated departed driver {name}."
                )
            else:
                conn.execute(
                    "UPDATE drivers SET name = ?, license_number = ?, contact_info = ?, status = ?, "
                    "is_terminated = 0, terminated_at = NULL, termination_reason = NULL "
                    "WHERE driver_id = ?",
                    (name, license_number, contact_info or None, status, id)
                )
                action = "Driver Reinstated" if was_terminated else "Driver Updated"
                details = (
                    f"Reinstated driver {name} as {status}."
                    if was_terminated else f"Updated driver {name}."
                )
            _record_user_activity(conn, action, details)
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            flash("That license number is already registered to another driver.", "error")
            return redirect(url_for("edit_driver", id=id))
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        flash(
            f"Driver {name} marked as departed. Their documents and trip history are retained."
            if status == "Departed" else f"Driver {name} updated successfully.",
            "success"
        )
        return redirect(url_for("drivers"))

    conn.close()
    return render_template(
        "driver_form.html",
        driver=driver,
        documents=documents,
        csrf_token=session["csrf_token"],
        block_content="dashboard"
    )


@app.route("/drivers/<int:id>/documents", methods=["POST"])
@admin_required
def upload_driver_document(id):
    if not _valid_csrf_token():
        flash("Your session token is invalid. Reload the page and try again.", "error")
        return redirect(url_for("edit_driver", id=id))

    document_type = request.form.get("document_type", "")
    uploaded_file = request.files.get("document")
    if document_type not in DRIVER_DOCUMENT_TYPES:
        flash("Choose a valid government document type.", "error")
        return redirect(url_for("edit_driver", id=id))
    if not uploaded_file or not uploaded_file.filename:
        flash("Choose a driver document to upload.", "error")
        return redirect(url_for("edit_driver", id=id))

    conn = get_db()
    driver = conn.execute(
        "SELECT name FROM drivers WHERE driver_id = ?",
        (id,)
    ).fetchone()
    if not driver:
        conn.close()
        return "Driver not found.", 404

    saved_path = None
    try:
        original_name, storage_name, file_size = _store_driver_document(uploaded_file)
        saved_path = Path(app.instance_path) / "driver_documents" / storage_name
        conn.execute(
            "INSERT INTO driver_documents "
            "(driver_id, document_type, original_name, storage_name, file_size, uploaded_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (id, document_type, original_name, storage_name, file_size, session["user_id"])
        )
        _record_user_activity(
            conn,
            "Driver Document Uploaded",
            f"Uploaded {document_type} for driver {driver['name']}."
        )
        conn.commit()
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("edit_driver", id=id))
    except Exception:
        conn.rollback()
        if saved_path:
            saved_path.unlink(missing_ok=True)
        raise
    finally:
        conn.close()

    flash(f"{document_type} uploaded for {driver['name']}.", "success")
    return redirect(url_for("edit_driver", id=id))


@app.route("/drivers/documents/<int:document_id>/download")
@admin_required
def download_driver_document(document_id):
    conn = get_db()
    document = conn.execute(
        "SELECT driver_documents.storage_name, driver_documents.original_name "
        "FROM driver_documents JOIN drivers ON drivers.driver_id = driver_documents.driver_id "
        "WHERE driver_documents.document_id = ?",
        (document_id,)
    ).fetchone()
    conn.close()
    if not document:
        abort(404)

    storage_name = document["storage_name"]
    if not re.fullmatch(r"[a-f0-9]{48}\.(pdf|jpg|jpeg|png)", storage_name):
        abort(404)
    storage_dir = (Path(app.instance_path) / "driver_documents").resolve()
    file_path = (storage_dir / storage_name).resolve()
    if file_path.parent != storage_dir or not file_path.is_file():
        abort(404)
    response = send_file(
        file_path,
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=document["original_name"],
        conditional=True
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/parties", methods=["GET", "POST"])
@access_required
def parties():
    """Party register with a small, auditable debit/credit ledger."""
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    conn = get_db()
    if request.method == "POST":
        if not _valid_csrf_token():
            conn.close()
            flash("Your session token is invalid. Reload the page and try again.", "error")
            return redirect(url_for("parties"))
        name = request.form.get("name", "").strip()
        party_type = request.form.get("party_type", "Customer").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip().lower()
        gstin = request.form.get("gstin", "").strip().upper()
        address = request.form.get("address", "").strip()
        notes = request.form.get("notes", "").strip()
        if not name or len(name) > 160:
            flash("Enter a party name (up to 160 characters).", "error")
        elif party_type not in ("Customer", "Supplier", "Both"):
            flash("Choose a valid party type.", "error")
        elif email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            flash("Enter a valid email address.", "error")
        elif gstin and not re.fullmatch(r"[0-9A-Z]{15}", gstin):
            flash("GSTIN must contain 15 letters or digits.", "error")
        else:
            try:
                conn.execute(
                    "INSERT INTO parties (owner_user_id, name, party_type, phone, email, gstin, address, notes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (session["user_id"], name, party_type, phone or None, email or None,
                     gstin or None, address or None, notes or None)
                )
                conn.commit()
                flash(f"Party '{name}' added.", "success")
            except sqlite3.IntegrityError:
                flash("A party with this name already exists in your account.", "error")
        conn.close()
        return redirect(url_for("parties"))

    where, params = _record_owner_filter("parties")
    rows = conn.execute(
        "SELECT p.party_id, p.name, p.party_type, p.phone, p.email, p.gstin, p.created_at, "
        "COALESCE(SUM(CASE WHEN e.entry_type = 'Debit' THEN e.amount ELSE -e.amount END), 0) AS balance, "
        "COUNT(e.entry_id) AS entry_count "
        "FROM parties p LEFT JOIN party_ledger_entries e ON e.party_id = p.party_id "
        f"WHERE {where.replace('parties.', 'p.')} "
        "GROUP BY p.party_id ORDER BY p.name COLLATE NOCASE",
        params
    ).fetchall()
    totals = conn.execute(
        "SELECT COUNT(*) AS party_count, "
        "COALESCE(SUM(CASE WHEN e.entry_type = 'Debit' THEN e.amount ELSE -e.amount END), 0) AS net_balance "
        "FROM parties p LEFT JOIN party_ledger_entries e ON e.party_id = p.party_id "
        f"WHERE {where.replace('parties.', 'p.')}",
        params
    ).fetchone()
    conn.close()
    return render_template(
        "parties.html", parties=rows, totals=totals,
        csrf_token=session["csrf_token"], today=date.today().isoformat(),
        block_content="dashboard"
    )


@app.route("/parties/<int:party_id>", methods=["GET", "POST"])
@access_required
def party_ledger(party_id):
    """Show one party's ledger and record debit/credit entries."""
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    conn = get_db()
    party_scope, scope_params = _record_owner_filter("parties")
    party = conn.execute(
        "SELECT p.* FROM parties p WHERE p.party_id = ? AND "
        + party_scope.replace("parties.", "p."),
        [party_id] + scope_params
    ).fetchone()
    if party is None:
        conn.close()
        abort(404)

    if request.method == "POST":
        if not _valid_csrf_token():
            conn.close()
            flash("Your session token is invalid. Reload the page and try again.", "error")
            return redirect(url_for("party_ledger", party_id=party_id))
        entry_date = request.form.get("entry_date", "").strip()
        entry_type = request.form.get("entry_type", "").strip()
        amount_text = request.form.get("amount", "").strip()
        reference = request.form.get("reference", "").strip()
        description = request.form.get("description", "").strip()
        try:
            entry_date = date.fromisoformat(entry_date).isoformat()
            amount = Decimal(amount_text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if not amount.is_finite() or amount <= 0 or amount > Decimal("999999999999.99"):
                raise ValueError
            if entry_type not in ("Debit", "Credit"):
                raise ValueError
            if len(reference) > 120 or len(description) > 500:
                raise ValueError
        except (ValueError, InvalidOperation):
            conn.close()
            flash("Enter a valid date, Debit/Credit type, positive amount, and short reference/description.", "error")
            return redirect(url_for("party_ledger", party_id=party_id))
        conn.execute(
            "INSERT INTO party_ledger_entries "
            "(party_id, entry_date, entry_type, amount, reference, description, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (party_id, entry_date, entry_type, float(amount), reference or None,
             description or None, session["user_id"])
        )
        conn.commit()
        log_activity(session["user_id"], "Party Ledger Entry", 
                     f"{entry_type} ₹{amount:.2f} for party #{party_id}", session["user_id"])
        conn.close()
        flash("Ledger entry recorded.", "success")
        return redirect(url_for("party_ledger", party_id=party_id))

    entries = conn.execute(
        "SELECT entry_id, entry_date, entry_type, amount, reference, description, trip_id "
        "FROM party_ledger_entries WHERE party_id = ? "
        "ORDER BY entry_date DESC, entry_id DESC",
        (party_id,)
    ).fetchall()
    balance = sum((Decimal(str(row["amount"])) * (1 if row["entry_type"] == "Debit" else -1)
                   for row in entries), Decimal("0.00"))
    debit_total = sum((Decimal(str(row["amount"])) for row in entries if row["entry_type"] == "Debit"), Decimal("0.00"))
    credit_total = sum((Decimal(str(row["amount"])) for row in entries if row["entry_type"] == "Credit"), Decimal("0.00"))
    conn.close()
    return render_template(
        "party_ledger.html", party=party, entries=entries, balance=balance,
        debit_total=debit_total, credit_total=credit_total,
        csrf_token=session["csrf_token"], today=date.today().isoformat(),
        block_content="dashboard"
    )


@app.route("/trips")
@access_required
def trips():
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    conn = get_db()
    if not _is_admin_user() and not session.get("driver_id"):
        conn.close()
        flash("Your account is not linked to a driver yet. Contact an administrator.", "info")
        return redirect(url_for("account"))
    trip_scope, trip_scope_params = _record_owner_filter("trips")
    rows = conn.execute(
        "SELECT trips.trip_id, trips.start_date, trips.end_date, trips.route_from, "
        "trips.route_to, trips.status, trips.freight_amount, drivers.name AS driver_name, "
        "trucks.registration_number, COALESCE(payments.amount_received, 0) AS amount_received, "
        "COALESCE(costs.approved_expenses, 0) AS approved_expenses, "
        "COALESCE(costs.pending_expenses, 0) AS pending_expenses "
        "FROM trips "
        "LEFT JOIN drivers ON drivers.driver_id = trips.driver_id "
        "LEFT JOIN trucks ON trucks.truck_id = trips.truck_id "
        "LEFT JOIN (SELECT trip_id, SUM(amount) AS amount_received "
        "FROM trip_payments GROUP BY trip_id) AS payments ON payments.trip_id = trips.trip_id "
        "LEFT JOIN (SELECT trip_id, "
        "SUM(CASE WHEN status = 'Approved' THEN amount ELSE 0 END) AS approved_expenses, "
        "SUM(CASE WHEN status = 'Pending' THEN amount ELSE 0 END) AS pending_expenses "
        "FROM expenses GROUP BY trip_id) AS costs ON costs.trip_id = trips.trip_id "
        f"WHERE {trip_scope} ORDER BY trips.trip_id DESC",
        trip_scope_params
    ).fetchall()
    counts = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status IN ('Scheduled', 'In Progress') THEN 1 ELSE 0 END) AS active, "
        "SUM(CASE WHEN status = 'Completed' THEN 1 ELSE 0 END) AS completed "
        f"FROM trips WHERE {trip_scope}",
        trip_scope_params
    ).fetchone()
    active_truck_ids = conn.execute(
        "SELECT DISTINCT truck_id FROM trips WHERE status IN ('Scheduled', 'In Progress')"
    ).fetchall()
    active_driver_ids = conn.execute(
        "SELECT DISTINCT driver_id FROM trips WHERE status IN ('Scheduled', 'In Progress')"
    ).fetchall()
    active_truck_ids = {row['truck_id'] for row in active_truck_ids}
    active_driver_ids = {row['driver_id'] for row in active_driver_ids}
    available_trucks = [
        dict(row) for row in conn.execute(
            "SELECT truck_id, registration_number, model FROM trucks "
            "WHERE status = 'Available' ORDER BY registration_number"
        ).fetchall()
        if row['truck_id'] not in active_truck_ids
    ]
    available_drivers = [
        dict(row) for row in conn.execute(
            "SELECT driver_id, name FROM drivers "
            "WHERE status = 'Active' AND is_terminated = 0 "
            "ORDER BY name COLLATE NOCASE"
        ).fetchall()
        if row['driver_id'] not in active_driver_ids
    ]
    assignable_users = _load_assignable_users(conn) if _is_admin_user() else []
    available_parties = conn.execute(
        "SELECT party_id, owner_user_id, name, party_type FROM parties "
        "ORDER BY name COLLATE NOCASE"
    ).fetchall() if _is_admin_user() else []
    conn.close()
    return render_template(
        "trips.html",
        trips=rows,
        counts=counts,
        available_trucks=available_trucks,
        available_drivers=available_drivers,
        assignable_users=assignable_users,
        available_parties=available_parties,
        current_user_id=session["user_id"],
        has_driver_link=bool(session.get("driver_id") or _is_admin_user()),
        today=date.today().isoformat(),
        can_dispatch=_is_admin_user(),
        csrf_token=session.get('csrf_token'),
        block_content='dashboard'
    )


@app.route("/payments")
@access_required
def payments():
    if not _is_admin_user():
        abort(403)
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    selected_trip_id = request.args.get("trip", type=int)
    due_only = request.args.get("due") == "1"
    conn = get_db()
    trip_scope, trip_scope_params = _record_owner_filter("trips")
    payment_rows = conn.execute(
        "SELECT trip_payments.payment_id, trip_payments.trip_id, trip_payments.amount, "
        "trip_payments.payment_date, trip_payments.recorded_at, trip_payments.reference, "
        "users.full_name AS recorder_name, users.email AS recorder_email "
        "FROM trip_payments "
        "JOIN trips ON trips.trip_id = trip_payments.trip_id "
        "LEFT JOIN users ON users.user_id = trip_payments.recorded_by "
        f"WHERE {trip_scope} "
        "ORDER BY trip_payments.recorded_at DESC, trip_payments.payment_id DESC",
        trip_scope_params
    ).fetchall()
    totals = conn.execute(
        "SELECT COUNT(DISTINCT trip_payments.trip_id) AS trip_count, "
        "COALESCE(SUM(trip_payments.amount), 0) AS amount_received "
        "FROM trip_payments "
        "JOIN trips ON trips.trip_id = trip_payments.trip_id "
        f"WHERE trips.status != 'Cancelled' AND {trip_scope}",
        trip_scope_params
    ).fetchone()
    payment_trip_rows = conn.execute(
        "SELECT trips.trip_id, trips.route_from, trips.route_to, trips.freight_amount, "
        "trips.status, trucks.registration_number, "
        "COALESCE(payments.amount_received, 0) AS amount_received "
        "FROM trips "
        "LEFT JOIN trucks ON trucks.truck_id = trips.truck_id "
        "LEFT JOIN (SELECT trip_id, SUM(amount) AS amount_received "
        "FROM trip_payments GROUP BY trip_id) AS payments ON payments.trip_id = trips.trip_id "
        f"WHERE trips.status != 'Cancelled' AND {trip_scope} "
        "ORDER BY trips.trip_id DESC",
        trip_scope_params
    ).fetchall()
    payment_history = conn.execute(
        "SELECT financial_corrections.correction_id, financial_corrections.record_id, "
        "financial_corrections.old_amount, financial_corrections.new_amount, "
        "financial_corrections.reason, financial_corrections.changed_at, "
        "users.full_name AS changed_by_name, users.email AS changed_by_email "
        "FROM financial_corrections "
        "LEFT JOIN users ON users.user_id = financial_corrections.changed_by "
        "WHERE financial_corrections.record_type = 'Payment' "
        "ORDER BY financial_corrections.correction_id DESC"
    ).fetchall()
    payments_by_trip = {}
    for payment in payment_rows:
        payment = dict(payment)
        payment["corrections"] = []
        payments_by_trip.setdefault(payment["trip_id"], []).append(payment)
    payments_by_id = {
        payment["payment_id"]: payment
        for payments_for_trip in payments_by_trip.values()
        for payment in payments_for_trip
    }
    for correction in payment_history:
        payment = payments_by_id.get(correction["record_id"])
        if payment:
            payment["corrections"].append(dict(correction))
    trip_totals = []
    for row in payment_trip_rows:
        trip = dict(row)
        trip["payments"] = payments_by_trip.get(trip["trip_id"], [])
        trip["balance_due"] = max(
            Decimal(str(trip["freight_amount"] or 0))
            - Decimal(str(trip["amount_received"] or 0)),
            Decimal("0")
        )
        trip["payment_status"] = (
            "Paid" if trip["balance_due"] == 0
            else "Partially paid" if Decimal(str(trip["amount_received"] or 0)) > 0
            else "Due"
        )
        if due_only and trip["balance_due"] <= 0:
            continue
        if selected_trip_id is not None and trip["trip_id"] != selected_trip_id:
            continue
        trip_totals.append(trip)
    if selected_trip_id is not None and not due_only:
        selected_trip = trip_totals[0] if trip_totals else None
        totals = {
            "trip_count": int(bool(selected_trip and selected_trip["payments"])),
            "amount_received": selected_trip["amount_received"] if selected_trip else Decimal("0.00"),
        }
    outstanding_due_total = sum(
        (trip["balance_due"] for trip in trip_totals), Decimal("0.00")
    )
    can_correct_payments = _is_admin_user()
    conn.close()
    return render_template(
        "payments.html",
        totals=totals,
        trip_totals=trip_totals,
        due_only=due_only,
        selected_trip_id=selected_trip_id,
        outstanding_due_total=outstanding_due_total,
        can_correct_payments=can_correct_payments,
        can_record_payments=can_correct_payments,
        today=date.today().isoformat(),
        csrf_token=session["csrf_token"],
        block_content="dashboard"
    )


def _valid_csrf_token():
    submitted_token = request.form.get("csrf_token", "")
    session_token = session.get("csrf_token", "")
    return bool(
        submitted_token
        and session_token
        and secrets.compare_digest(submitted_token, session_token)
    )


def _get_trip_financials(conn, owner_user_id=None):
    owner_filter = ""
    owner_params = []
    if owner_user_id == "scoped":
        owner_filter, owner_params = _record_owner_filter("trips")
        owner_filter = f"WHERE {owner_filter} "
    elif owner_user_id is not None:
        owner_filter = "WHERE trips.owner_user_id = ? "
        owner_params.append(owner_user_id)
    rows = conn.execute(
        "SELECT trips.trip_id, trips.route_from, trips.route_to, trips.start_date, "
        "trips.status, trips.freight_amount, trucks.registration_number, "
        "COALESCE(payments.amount_received, 0) AS amount_received, "
        "COALESCE(costs.approved_expenses, 0) AS approved_expenses, "
        "COALESCE(costs.pending_expenses, 0) AS pending_expenses "
        "FROM trips "
        "LEFT JOIN trucks ON trucks.truck_id = trips.truck_id "
        "LEFT JOIN (SELECT trip_id, SUM(amount) AS amount_received "
        "FROM trip_payments GROUP BY trip_id) AS payments ON payments.trip_id = trips.trip_id "
        "LEFT JOIN (SELECT trip_id, "
        "SUM(CASE WHEN status = 'Approved' THEN amount ELSE 0 END) AS approved_expenses, "
        "SUM(CASE WHEN status = 'Pending' THEN amount ELSE 0 END) AS pending_expenses "
        "FROM expenses GROUP BY trip_id) AS costs ON costs.trip_id = trips.trip_id "
        f"{owner_filter}"
        "ORDER BY trips.trip_id DESC",
        owner_params
    ).fetchall()

    zero = Decimal("0.00")
    trip_finances = []
    summary = {
        "trip_count": len(rows),
        "freight_total": zero,
        "received_total": zero,
        "balance_due_total": zero,
        "approved_expenses_total": zero,
        "pending_expenses_total": zero,
        "profit_loss_total": zero,
        "cash_profit_loss_total": zero,
    }
    for row in rows:
        trip = dict(row)
        for key in ("freight_amount", "amount_received", "approved_expenses", "pending_expenses"):
            trip[key] = Decimal(str(trip[key] or 0)).quantize(Decimal("0.01"))
        trip["balance_due"] = max(trip["freight_amount"] - trip["amount_received"], zero)
        trip["profit_loss"] = trip["freight_amount"] - trip["approved_expenses"]
        trip["cash_profit_loss"] = trip["amount_received"] - trip["approved_expenses"]
        summary["freight_total"] += trip["freight_amount"]
        summary["received_total"] += trip["amount_received"]
        summary["balance_due_total"] += trip["balance_due"]
        summary["approved_expenses_total"] += trip["approved_expenses"]
        summary["pending_expenses_total"] += trip["pending_expenses"]
        summary["profit_loss_total"] += trip["profit_loss"]
        summary["cash_profit_loss_total"] += trip["cash_profit_loss"]
        trip_finances.append(trip)

    return summary, trip_finances


def _log_trip_action(conn, action, details, owner_user_id=None):
    owner_user_id = owner_user_id or session["user_id"]
    conn.execute(
        "INSERT INTO user_logs (user_id, action, details, actor_user_id) "
        "VALUES (?, ?, ?, ?)",
        (owner_user_id, action, details, session['user_id'])
    )


@app.route("/expenses")
@access_required
def expenses():
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    selected_trip_id = request.args.get("trip", type=int)
    selected_status = request.args.get("status", "")
    if selected_status not in {"Approved", "Pending", "Rejected"}:
        selected_status = ""

    expense_filters = []
    expense_params = []
    is_admin = _is_admin_user()
    trip_scope, trip_scope_params = _record_owner_filter("trips")
    expense_filters.append(trip_scope)
    expense_params.extend(trip_scope_params)
    if selected_trip_id is not None:
        expense_filters.append("expenses.trip_id = ?")
        expense_params.append(selected_trip_id)
    if selected_status:
        expense_filters.append("expenses.status = ?")
        expense_params.append(selected_status)
    expense_where = (
        "WHERE " + " AND ".join(expense_filters)
        if expense_filters else ""
    )

    conn = get_db()
    rows = conn.execute(
        "SELECT expenses.expense_id, expenses.trip_id, expenses.category, "
        "expenses.amount, expenses.date, expenses.description, expenses.status, "
        "expenses.reviewed_at, expenses.created_by, trips.route_from, trips.route_to, "
        "trucks.registration_number, drivers.name AS driver_name, "
        "submitter.full_name AS submitter_name, submitter.email AS submitter_email, "
        "reviewer.full_name AS reviewer_name, reviewer.email AS reviewer_email "
        "FROM expenses "
        "JOIN trips ON trips.trip_id = expenses.trip_id "
        "LEFT JOIN trucks ON trucks.truck_id = trips.truck_id "
        "LEFT JOIN drivers ON drivers.driver_id = trips.driver_id "
        "LEFT JOIN users AS submitter ON submitter.user_id = expenses.created_by "
        "LEFT JOIN users AS reviewer ON reviewer.user_id = expenses.reviewed_by "
        f"{expense_where} "
        "ORDER BY COALESCE(expenses.created_at, expenses.date) DESC, expenses.expense_id DESC",
        expense_params
    ).fetchall()
    corrections = conn.execute(
        "SELECT financial_corrections.correction_id, financial_corrections.record_id, "
        "financial_corrections.old_amount, financial_corrections.new_amount, "
        "financial_corrections.old_status, financial_corrections.new_status, "
        "financial_corrections.reason, financial_corrections.changed_at, "
        "users.full_name AS changed_by_name, users.email AS changed_by_email "
        "FROM financial_corrections "
        "LEFT JOIN users ON users.user_id = financial_corrections.changed_by "
        "WHERE financial_corrections.record_type = 'Expense' "
        "ORDER BY financial_corrections.correction_id DESC"
    ).fetchall()
    corrections_by_expense = {}
    for correction in corrections:
        corrections_by_expense.setdefault(correction["record_id"], []).append(dict(correction))
    expense_rows = []
    current_user_id = session["user_id"]
    for row in rows:
        expense = dict(row)
        expense["corrections"] = corrections_by_expense.get(expense["expense_id"], [])
        expense["can_correct"] = is_admin or expense["created_by"] == current_user_id
        expense_rows.append(expense)
    filtered_expense_total = sum(
        (Decimal(str(expense["amount"])) for expense in expense_rows),
        Decimal("0.00")
    )
    expenses_by_trip = {}
    for expense in expense_rows:
        trip_id = expense["trip_id"]
        trip_group = expenses_by_trip.get(trip_id)
        if trip_group is None:
            trip_group = {
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
            expenses_by_trip[trip_id] = trip_group
        amount = Decimal(str(expense["amount"]))
        trip_group["expenses"].append(expense)
        trip_group["total"] += amount
        if expense["status"] == "Approved":
            trip_group["approved_total"] += amount
        elif expense["status"] == "Pending":
            trip_group["pending_total"] += amount
        elif expense["status"] == "Rejected":
            trip_group["rejected_total"] += amount
    totals = conn.execute(
        "SELECT COUNT(*) AS count, "
        "COALESCE(SUM(CASE WHEN expenses.status = 'Pending' THEN expenses.amount ELSE 0 END), 0) AS pending, "
        "COALESCE(SUM(CASE WHEN expenses.status = 'Approved' THEN expenses.amount ELSE 0 END), 0) AS approved "
        "FROM expenses JOIN trips ON trips.trip_id = expenses.trip_id "
        f"WHERE {trip_scope}",
        trip_scope_params
    ).fetchone()
    finance_summary, trip_finances = _get_trip_financials(
        conn,
        None if session.get("email", "").lower() == SUPER_ADMIN_EMAIL.lower() else "scoped"
    )
    trips = conn.execute(
        "SELECT trips.trip_id, trips.route_from, trips.route_to, "
        "trucks.registration_number, drivers.name AS driver_name "
        "FROM trips "
        "LEFT JOIN trucks ON trucks.truck_id = trips.truck_id "
        "LEFT JOIN drivers ON drivers.driver_id = trips.driver_id "
        f"WHERE trips.status != 'Cancelled' AND {trip_scope} "
        "ORDER BY trips.trip_id DESC",
        trip_scope_params
    ).fetchall()
    conn.close()
    return render_template(
        "expenses.html",
        expenses=expense_rows,
        expenses_by_trip=list(expenses_by_trip.values()),
        selected_trip_id=selected_trip_id,
        selected_status=selected_status,
        filtered_expense_total=filtered_expense_total,
        totals=totals,
        finance_summary=finance_summary,
        trip_finances=trip_finances,
        trips=trips,
        today=date.today().isoformat(),
        csrf_token=session["csrf_token"],
        can_review=bool(session.get("is_admin") or session.get("role") == "Admin"),
        block_content="dashboard"
    )


@app.route("/expenses/create", methods=["POST"])
@access_required
def create_expense():
    if not _valid_csrf_token():
        flash("Your session token is invalid. Reload the expenses page and try again.", "error")
        return redirect(url_for("expenses"))

    try:
        trip_id = int(request.form.get("trip_id", ""))
        amount = float(request.form.get("amount", ""))
        expense_date = request.form.get("date", "").strip()
        datetime.strptime(expense_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        flash("Enter a valid trip, date, and amount.", "error")
        return redirect(url_for("expenses"))

    category = request.form.get("category", "")
    description = request.form.get("description", "").strip()
    if category not in {
        "Fuel", "Toll", "Meals/Allowance", "Maintenance/Repairs",
        "Loading/Unloading", "Other", "Food", "Maintenance"
    }:
        flash("Choose a valid expense category.", "error")
        return redirect(url_for("expenses"))
    if not 0 < amount <= 100000000:
        flash("Amount must be greater than zero and no more than 100,000,000.", "error")
        return redirect(url_for("expenses"))
    if len(description) > 500:
        flash("Notes must be 500 characters or fewer.", "error")
        return redirect(url_for("expenses"))

    conn = get_db()
    trip_owner_clause, trip_owner_params = _record_owner_filter("trips")
    trip = conn.execute(
        "SELECT trip_id, owner_user_id FROM trips WHERE trip_id = ? AND status != 'Cancelled' "
        f"AND {trip_owner_clause}",
        [trip_id, *trip_owner_params]
    ).fetchone()
    if not trip:
        conn.close()
        flash("Choose an existing, non-cancelled trip.", "error")
        return redirect(url_for("expenses"))
    is_admin = _is_admin_user()
    expense_status = "Approved" if is_admin else "Pending"
    reviewer_id = session["user_id"] if is_admin else None
    cursor = conn.execute(
        "INSERT INTO expenses "
        "(trip_id, category, amount, date, description, created_at, created_by, "
        "status, reviewed_by, reviewed_at) "
        "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, "
        "CASE WHEN ? = 'Approved' THEN CURRENT_TIMESTAMP ELSE NULL END)",
        (
            trip_id, category, amount, expense_date, description,
            session["user_id"], expense_status, reviewer_id, expense_status
        )
    )
    expense_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO user_logs (user_id, action, details, actor_user_id) "
        "VALUES (?, ?, ?, ?)",
        (
            trip["owner_user_id"],
            "Expense Recorded" if is_admin else "Expense Submitted",
            f"Expense #{expense_id} recorded for trip #{trip_id}: {category}, "
            f"{amount:.2f}, status {expense_status.lower()}.",
            session["user_id"]
        )
    )
    conn.commit()
    conn.close()
    flash(
        f"Expense #{expense_id} recorded and approved."
        if is_admin else f"Expense #{expense_id} submitted for admin review.",
        "success"
    )
    return redirect(url_for("expenses"))


@app.route("/expenses/<int:id>/amount", methods=["POST"])
@access_required
def correct_expense_amount(id):
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
        expense = conn.execute(
            "SELECT expenses.trip_id, expenses.amount, expenses.status, expenses.created_by, "
            "trips.owner_user_id FROM expenses JOIN trips ON trips.trip_id = expenses.trip_id "
            "WHERE expenses.expense_id = ? AND " + _record_owner_filter("trips")[0],
            [id, *_record_owner_filter("trips")[1]]
        ).fetchone()
        is_admin = _is_admin_user()
        if not expense or not (
            is_admin or expense["created_by"] == session["user_id"]
        ):
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
                new_status, reason, session["user_id"]
            )
        )
        conn.execute(
            "UPDATE expenses SET amount = ?, status = ?, reviewed_by = ?, "
            "reviewed_at = CASE WHEN ? = 'Approved' THEN CURRENT_TIMESTAMP ELSE NULL END "
            "WHERE expense_id = ?",
            (float(new_amount), new_status, reviewer_id, new_status, id)
        )
        conn.execute(
            "INSERT INTO user_logs (user_id, action, details, actor_user_id) "
            "VALUES (?, 'Expense Amount Corrected', ?, ?)",
            (
                expense["owner_user_id"],
                f"Corrected expense #{id} on trip #{expense['trip_id']} from "
                f"{old_amount:.2f} to {new_amount:.2f}. Reason: {reason}",
                session["user_id"]
            )
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    flash(
        f"Expense #{id} corrected and approved."
        if is_admin else f"Expense #{id} corrected and sent for admin review.",
        "success"
    )
    return redirect(url_for("expenses"))


@app.route("/expenses/<int:id>/review", methods=["POST"])
@admin_required
def review_expense(id):
    if not _valid_csrf_token():
        flash("Your session token is invalid. Reload the expenses page and try again.", "error")
        return redirect(url_for("expenses"))

    decision = request.form.get("status", "")
    if decision not in {"Approved", "Rejected"}:
        flash("Choose approve or reject.", "error")
        return redirect(url_for("expenses"))

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        expense = conn.execute(
            "SELECT expenses.trip_id, expenses.category, expenses.amount, trips.owner_user_id "
            "FROM expenses JOIN trips ON trips.trip_id = expenses.trip_id "
            "WHERE expenses.expense_id = ? AND expenses.status = 'Pending' AND "
            + _record_owner_filter("trips")[0],
            [id, *_record_owner_filter("trips")[1]]
        ).fetchone()
        if not expense:
            conn.rollback()
            flash("This expense is no longer awaiting review.", "error")
            return redirect(url_for("expenses"))
        updated = conn.execute(
            "UPDATE expenses SET status = ?, reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP "
            "WHERE expense_id = ? AND status = 'Pending'",
            (decision, session["user_id"], id)
        )
        if updated.rowcount != 1:
            conn.rollback()
            flash("This expense is no longer awaiting review.", "error")
            return redirect(url_for("expenses"))
        conn.execute(
            "INSERT INTO user_logs (user_id, action, details, actor_user_id) "
            "VALUES (?, 'Expense Reviewed', ?, ?)",
            (
                expense["owner_user_id"],
                f"Expense #{id} for trip #{expense['trip_id']} ({expense['category']}, "
                f"{expense['amount']:.2f}) was {decision.lower()}.",
                session["user_id"]
            )
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    flash(f"Expense #{id} {decision.lower()}.", "success")
    return redirect(url_for("expenses"))


@app.route("/parties/quick-create", methods=["POST"])
@access_required
def quick_create_party():
    """Create a minimal party from the Add Trip workflow."""
    if not _is_admin_user():
        return jsonify({"ok": False, "error": "Administrator access is required to add a party."}), 403
    if not _valid_csrf_token():
        return jsonify({"ok": False, "error": "Your session token is invalid. Reload the page and try again."}), 400
    name = request.form.get("name", "").strip()
    party_type = request.form.get("party_type", "Customer").strip()
    phone = request.form.get("phone", "").strip()
    if not name or len(name) > 160:
        return jsonify({"ok": False, "error": "Enter a party name (up to 160 characters)."}), 400
    if party_type not in ("Customer", "Supplier", "Both"):
        return jsonify({"ok": False, "error": "Choose Customer, Supplier, or Both."}), 400
    if len(phone) > 40:
        return jsonify({"ok": False, "error": "Phone number is too long."}), 400
    try:
        owner_user_id = int(request.form.get("owner_user_id") or session["user_id"])
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Choose a valid trip owner."}), 400
    conn = get_db()
    try:
        if not _is_assignable_owner(conn, owner_user_id):
            return jsonify({"ok": False, "error": "Choose an active trip owner."}), 400
        existing = conn.execute(
            "SELECT party_id, name, party_type FROM parties WHERE owner_user_id = ? AND name = ? COLLATE NOCASE",
            (owner_user_id, name),
        ).fetchone()
        if existing:
            return jsonify({"ok": True, "party": {"party_id": existing["party_id"], "name": existing["name"],
                "party_type": existing["party_type"]}, "existing": True})
        cursor = conn.execute(
            "INSERT INTO parties (owner_user_id, name, party_type, phone) VALUES (?, ?, ?, ?)",
            (owner_user_id, name, party_type, phone or None),
        )
        conn.commit()
        return jsonify({"ok": True, "party": {"party_id": cursor.lastrowid, "name": name,
            "party_type": party_type}, "existing": False}), 201
    except sqlite3.IntegrityError:
        conn.rollback()
        return jsonify({"ok": False, "error": "This party already exists for the selected account."}), 409
    finally:
        conn.close()


@app.route("/trips/create", methods=["POST"])
@access_required
def create_trip():
    if not _is_admin_user():
        return "Access denied: administrator privileges required.", 403
    if not _valid_csrf_token():
        flash("Your session token is invalid. Reload the trips page and try again.", "error")
        return redirect(url_for("trips"))

    route_from = request.form.get("route_from", "").strip()
    route_to = request.form.get("route_to", "").strip()
    start_date = request.form.get("start_date", "").strip()
    truck_id = request.form.get("truck_id", "").strip()
    driver_id = request.form.get("driver_id", "").strip()
    trip_owner_id = request.form.get("owner_user_id", str(session["user_id"]))
    raw_party_id = request.form.get("party_id", "").strip()
    raw_freight_amount = request.form.get("freight_amount", "").strip()
    try:
        party_id = int(raw_party_id) if raw_party_id else None
    except ValueError:
        flash("Choose a valid party.", "error")
        return redirect(url_for("trips"))

    try:
        truck_id = int(truck_id)
        driver_id = int(driver_id)
        trip_owner_id = int(trip_owner_id)
        datetime.strptime(start_date, "%Y-%m-%d")
        freight_amount = Decimal(raw_freight_amount).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except (TypeError, ValueError, InvalidOperation):
        flash("Choose a valid date, truck, and driver.", "error")
        return redirect(url_for("trips"))

    if not freight_amount.is_finite() or not Decimal("0") < freight_amount <= Decimal("100000000"):
        flash("Enter a freight charge greater than zero and no more than 100,000,000.", "error")
        return redirect(url_for("trips"))
    if not route_from or not route_to or len(route_from) > 120 or len(route_to) > 120:
        flash("Enter both route locations (up to 120 characters each).", "error")
        return redirect(url_for("trips"))

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not _is_admin_user():
            trip_owner_id = session["user_id"]
        elif not _is_assignable_owner(conn, trip_owner_id):
            conn.rollback()
            flash("Assign the trip to an active user account.", "error")
            return redirect(url_for("trips"))
        trip_owner = conn.execute(
            "SELECT role, driver_id FROM users WHERE user_id = ? "
            "AND is_approved = 1 AND is_rejected = 0 AND is_terminated = 0",
            (trip_owner_id,)
        ).fetchone()
        if not trip_owner or (
            trip_owner["role"] == "User"
            and trip_owner["driver_id"] != driver_id
        ):
            conn.rollback()
            flash("A Driver account can only be assigned trips for its linked driver record.", "error")
            return redirect(url_for("trips"))
        if party_id is not None:
            party = conn.execute(
                "SELECT party_id, owner_user_id, name FROM parties WHERE party_id = ?",
                (party_id,)
            ).fetchone()
            if not party or party["owner_user_id"] != trip_owner_id:
                conn.rollback()
                flash("Choose a party belonging to the selected trip account.", "error")
                return redirect(url_for("trips"))
        else:
            party = None
        truck = conn.execute(
            "SELECT registration_number FROM trucks "
            "WHERE truck_id = ? AND status = 'Available'",
            (truck_id,)
        ).fetchone()
        driver = conn.execute(
            "SELECT name FROM drivers "
            "WHERE driver_id = ? AND status = 'Active' AND is_terminated = 0",
            (driver_id,)
        ).fetchone()
        assigned = conn.execute(
            "SELECT 1 FROM trips WHERE status IN ('Scheduled', 'In Progress') "
            "AND (truck_id = ? OR driver_id = ?) LIMIT 1",
            (truck_id, driver_id)
        ).fetchone()
        if not truck or not driver or assigned:
            conn.rollback()
            flash("That truck or driver is no longer available. Refresh and choose another.", "error")
            return redirect(url_for("trips"))

        cursor = conn.execute(
            "INSERT INTO trips "
            "(truck_id, driver_id, start_date, route_from, route_to, freight_amount, status, owner_user_id, party_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 'Scheduled', ?, ?)",
            (truck_id, driver_id, start_date, route_from, route_to, float(freight_amount), trip_owner_id, party_id)
        )
        trip_id = cursor.lastrowid
        if party is not None:
            conn.execute(
                "INSERT INTO party_ledger_entries "
                "(party_id, entry_date, entry_type, amount, reference, description, trip_id, created_by) "
                "VALUES (?, ?, 'Debit', ?, ?, ?, ?, ?)",
                (party_id, start_date, float(freight_amount), f"TRIP-{trip_id}",
                 f"Freight billed for Trip #{trip_id}", trip_id, session["user_id"])
            )
        _log_trip_action(
            conn,
            "Trip Scheduled",
            f"Trip #{trip_id}: {route_from} to {route_to}; "
            f"truck {truck['registration_number']}; driver {driver['name']}; "
            f"freight charge {freight_amount:.2f}.",
            trip_owner_id
        )
        conn.commit()
        flash(f"Trip #{trip_id} scheduled.", "success")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return redirect(url_for("trips"))


@app.route("/trips/<int:id>/freight", methods=["POST"])
@access_required
def update_trip_freight(id):
    if not _is_admin_user():
        return "Access denied: administrator privileges required.", 403
    if not _valid_csrf_token():
        flash("Your session token is invalid. Reload the trips page and try again.", "error")
        return redirect(url_for("trips"))

    try:
        freight_amount = Decimal(request.form.get("freight_amount", "").strip()).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except (TypeError, InvalidOperation):
        flash("Enter a valid freight charge.", "error")
        return redirect(url_for("trips"))
    if not freight_amount.is_finite() or not Decimal("0") < freight_amount <= Decimal("100000000"):
        flash("Freight charge must be greater than zero and no more than 100,000,000.", "error")
        return redirect(url_for("trips"))

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        trip = conn.execute(
            "SELECT freight_amount, route_from, route_to, owner_user_id, party_id FROM trips "
            "WHERE trip_id = ? AND " + _record_owner_filter("trips")[0],
            [id, *_record_owner_filter("trips")[1]]
        ).fetchone()
        if not trip:
            conn.rollback()
            flash("Trip not found.", "error")
            return redirect(url_for("trips"))
        received = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS amount_received "
            "FROM trip_payments WHERE trip_id = ?",
            (id,)
        ).fetchone()["amount_received"]
        if freight_amount < Decimal(str(received)):
            conn.rollback()
            flash("Freight charge cannot be less than payments already received.", "error")
            return redirect(url_for("trips"))
        conn.execute(
            "UPDATE trips SET freight_amount = ? WHERE trip_id = ?",
            (float(freight_amount), id)
        )
        conn.execute(
            "UPDATE party_ledger_entries SET amount = ?, description = ? "
            "WHERE trip_id = ? AND entry_type = 'Debit' AND reference = ?",
            (float(freight_amount), f"Freight billed for Trip #{id}", id, f"TRIP-{id}")
        )
        _log_trip_action(
            conn,
            "Trip Freight Updated",
            f"Updated trip #{id} freight charge from {trip['freight_amount']:.2f} "
            f"to {freight_amount:.2f}.",
            trip["owner_user_id"]
        )
        conn.commit()
        flash(f"Freight charge for trip #{id} updated.", "success")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return redirect(url_for("trips"))


@app.route("/trips/<int:id>/payments", methods=["POST"])
@access_required
def record_trip_payment(id):
    if not _is_admin_user():
        return "Access denied: administrator privileges required.", 403
    return_to_due = request.form.get("return_to") == "due"
    return_url = (
        url_for("payments", due="1", trip=id)
        if return_to_due else url_for("trips")
    )
    if not _valid_csrf_token():
        flash("Your session token is invalid. Reload the page and try again.", "error")
        return redirect(return_url)

    try:
        amount = Decimal(request.form.get("amount", "").strip()).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        payment_date = request.form.get("payment_date", "").strip()
        datetime.strptime(payment_date, "%Y-%m-%d")
    except (TypeError, ValueError, InvalidOperation):
        flash("Enter a valid payment date and amount.", "error")
        return redirect(return_url)
    if not amount.is_finite() or not Decimal("0") < amount <= Decimal("100000000"):
        flash("Payment must be greater than zero and no more than 100,000,000.", "error")
        return redirect(return_url)

    reference = request.form.get("reference", "").strip()
    if len(reference) > 200:
        flash("Payment reference must be 200 characters or fewer.", "error")
        return redirect(return_url)

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        trip = conn.execute(
            "SELECT freight_amount, route_from, route_to, owner_user_id, party_id FROM trips "
            "WHERE trip_id = ? AND " + _record_owner_filter("trips")[0],
            [id, *_record_owner_filter("trips")[1]]
        ).fetchone()
        if not trip:
            conn.rollback()
            flash("Trip not found.", "error")
            return redirect(return_url)
        paid = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS amount_received "
            "FROM trip_payments WHERE trip_id = ?",
            (id,)
        ).fetchone()["amount_received"]
        due = Decimal(str(trip["freight_amount"])) - Decimal(str(paid))
        if amount > due:
            conn.rollback()
            flash(f"Payment exceeds the remaining balance of {max(due, Decimal('0')):.2f}.", "error")
            return redirect(return_url)

        payment_cursor = conn.execute(
            "INSERT INTO trip_payments "
            "(trip_id, amount, payment_date, reference, recorded_by) VALUES (?, ?, ?, ?, ?)",
            (id, float(amount), payment_date, reference, session["user_id"])
        )
        if trip["party_id"] is not None:
            conn.execute(
                "INSERT INTO party_ledger_entries "
                "(party_id, entry_date, entry_type, amount, reference, description, trip_id, created_by) "
                "VALUES (?, ?, 'Credit', ?, ?, ?, ?, ?)",
                (trip["party_id"], payment_date, float(amount),
                 f"PAY-{payment_cursor.lastrowid}",
                 f"Payment received for Trip #{id}" + (f" ({reference})" if reference else ""),
                 id, session["user_id"])
            )
        _log_trip_action(
            conn,
            "Trip Payment Recorded",
            f"Recorded payment {amount:.2f} for trip #{id} "
            f"({trip['route_from']} to {trip['route_to']})."
            + (f" Reference: {reference}" if reference else ""),
            trip["owner_user_id"]
        )
        conn.commit()
        flash(f"Payment of {amount:.2f} recorded for trip #{id}.", "success")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return redirect(return_url)


@app.route("/payments/<int:id>/amount", methods=["POST"])
@access_required
def correct_payment_amount(id):
    if not _is_admin_user():
        return "Access denied: administrator privileges required.", 403
    if not _valid_csrf_token():
        flash("Your session token is invalid. Reload the payments page and try again.", "error")
        return redirect(url_for("payments"))

    try:
        new_amount = Decimal(request.form.get("amount", "").strip()).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except (TypeError, InvalidOperation):
        flash("Enter a valid corrected payment amount.", "error")
        return redirect(url_for("payments"))
    if not new_amount.is_finite() or not Decimal("0") < new_amount <= Decimal("100000000"):
        flash("Corrected payment must be greater than zero and no more than 100,000,000.", "error")
        return redirect(url_for("payments"))

    reason = request.form.get("reason", "").strip()
    if not 5 <= len(reason) <= 300:
        flash("Explain the correction in 5 to 300 characters.", "error")
        return redirect(url_for("payments"))

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        payment = conn.execute(
            "SELECT trip_payments.payment_id, trip_payments.trip_id, trip_payments.amount, "
            "trips.freight_amount, trips.owner_user_id "
            "FROM trip_payments JOIN trips ON trips.trip_id = trip_payments.trip_id "
            "WHERE trip_payments.payment_id = ? AND " + _record_owner_filter("trips")[0],
            [id, *_record_owner_filter("trips")[1]]
        ).fetchone()
        if not payment:
            conn.rollback()
            flash("Payment not found.", "error")
            return redirect(url_for("payments"))

        old_amount = Decimal(str(payment["amount"])).quantize(Decimal("0.01"))
        if old_amount == new_amount:
            conn.rollback()
            flash("The corrected amount must differ from the current amount.", "error")
            return redirect(url_for("payments"))

        other_payments = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS amount_received "
            "FROM trip_payments WHERE trip_id = ? AND payment_id != ?",
            (payment["trip_id"], id)
        ).fetchone()["amount_received"]
        if Decimal(str(other_payments)) + new_amount > Decimal(str(payment["freight_amount"])):
            conn.rollback()
            flash("Corrected payment would exceed the trip's customer freight charge.", "error")
            return redirect(url_for("payments"))

        conn.execute(
            "INSERT INTO financial_corrections "
            "(record_type, record_id, old_amount, new_amount, reason, changed_by) "
            "VALUES ('Payment', ?, ?, ?, ?, ?)",
            (id, float(old_amount), float(new_amount), reason, session["user_id"])
        )
        conn.execute(
            "UPDATE trip_payments SET amount = ? WHERE payment_id = ?",
            (float(new_amount), id)
        )
        conn.execute(
            "UPDATE party_ledger_entries SET amount = ?, description = ? "
            "WHERE reference = ? AND entry_type = 'Credit' AND trip_id = ?",
            (float(new_amount), f"Payment received for Trip #{payment['trip_id']} (corrected)",
             f"PAY-{id}", payment["trip_id"])
        )
        conn.execute(
            "INSERT INTO user_logs (user_id, action, details, actor_user_id) "
            "VALUES (?, 'Payment Amount Corrected', ?, ?)",
            (
                payment["owner_user_id"],
                f"Corrected payment #{id} for trip #{payment['trip_id']} from "
                f"{old_amount:.2f} to {new_amount:.2f}. Reason: {reason}",
                session["user_id"]
            )
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    flash(f"Payment #{id} corrected. The original amount remains in its history.", "success")
    return redirect(url_for("payments"))


@app.route("/trips/<int:id>/status", methods=["POST"])
@access_required
def update_trip_status(id):
    if not _is_admin_user():
        return "Access denied: administrator privileges required.", 403
    if not _valid_csrf_token():
        flash("Your session token is invalid. Reload the trips page and try again.", "error")
        return redirect(url_for("trips"))

    requested_status = request.form.get("status", "")
    raw_odometer = request.form.get("odometer", "").strip()
    if requested_status not in {"In Progress", "Completed", "Cancelled"}:
        flash("That trip status is not allowed.", "error")
        return redirect(url_for("trips"))

    odometer = None
    if requested_status in {"In Progress", "Completed"}:
        try:
            odometer = int(raw_odometer)
            if odometer < 0:
                raise ValueError
        except ValueError:
            flash("Enter a valid non-negative odometer reading.", "error")
            return redirect(url_for("trips"))

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        trip = conn.execute(
            "SELECT trips.trip_id, trips.truck_id, trips.driver_id, trips.status, "
            "trips.start_odometer, trips.owner_user_id, trucks.registration_number "
            "FROM trips JOIN trucks ON trucks.truck_id = trips.truck_id "
            "WHERE trips.trip_id = ? AND " + _record_owner_filter("trips")[0],
            [id, *_record_owner_filter("trips")[1]]
        ).fetchone()
        if not trip:
            conn.rollback()
            flash("Trip not found.", "error")
            return redirect(url_for("trips"))

        current_status = trip['status']
        if requested_status == "In Progress" and current_status == "Scheduled":
            driver_available = conn.execute(
                "SELECT status FROM drivers WHERE driver_id = ?",
                (trip['driver_id'],)
            ).fetchone()
            conflicting_trip = conn.execute(
                "SELECT 1 FROM trips WHERE trip_id != ? "
                "AND status IN ('Scheduled', 'In Progress') "
                "AND (truck_id = ? OR driver_id = ?) LIMIT 1",
                (id, trip['truck_id'], trip['driver_id'])
            ).fetchone()
            if (
                not driver_available
                or driver_available['status'] != 'Active'
                or conflicting_trip
            ):
                conn.rollback()
                flash("The assigned truck or driver is no longer available.", "error")
                return redirect(url_for("trips"))

            conn.execute(
                "UPDATE trips SET status = 'In Progress', start_odometer = ? WHERE trip_id = ?",
                (odometer, id)
            )
            updated_truck = conn.execute(
                "UPDATE trucks SET status = 'In Trip' WHERE truck_id = ? AND status = 'Available'",
                (trip['truck_id'],)
            )
            if updated_truck.rowcount != 1:
                conn.rollback()
                flash("The assigned truck is no longer available.", "error")
                return redirect(url_for("trips"))
        elif requested_status == "Completed" and current_status == "In Progress":
            if trip['start_odometer'] is None or odometer < trip['start_odometer']:
                conn.rollback()
                flash("Ending odometer must be at least the starting odometer.", "error")
                return redirect(url_for("trips"))
            conn.execute(
                "UPDATE trips SET status = 'Completed', end_odometer = ?, "
                "end_date = ? WHERE trip_id = ?",
                (odometer, date.today().isoformat(), id)
            )
            conn.execute(
                "UPDATE trucks SET status = 'Available' "
                "WHERE truck_id = ? AND status = 'In Trip'",
                (trip['truck_id'],)
            )
        elif requested_status == "Cancelled" and current_status == "Scheduled":
            conn.execute(
                "UPDATE trips SET status = 'Cancelled' WHERE trip_id = ?",
                (id,)
            )
        else:
            conn.rollback()
            flash(f"A trip cannot move from {current_status} to {requested_status}.", "error")
            return redirect(url_for("trips"))

        _log_trip_action(
            conn,
            "Trip Status Updated",
            f"Trip #{id} for truck {trip['registration_number']} changed "
            f"from {current_status} to {requested_status}.",
            trip["owner_user_id"]
        )
        conn.commit()
        flash(f"Trip #{id} is now {requested_status.lower()}.", "success")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return redirect(url_for("trips"))


@app.route("/maintenance")
@access_required
def maintenance():
    if not _is_admin_user():
        abort(403)
    conn = get_db()
    owner_clause, owner_params = _record_owner_filter("maintenance")
    rows = conn.execute(
        "SELECT maintenance.maintenance_id, maintenance.service_date, "
        "maintenance.service_type, maintenance.cost, maintenance.notes, "
        "trucks.registration_number "
        "FROM maintenance "
        "LEFT JOIN trucks ON trucks.truck_id = maintenance.truck_id "
        f"WHERE {owner_clause} ORDER BY maintenance.service_date DESC, maintenance.maintenance_id DESC",
        owner_params
    ).fetchall()
    counts = conn.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(cost), 0) AS total_cost "
        f"FROM maintenance WHERE {owner_clause}",
        owner_params
    ).fetchone()
    conn.close()
    return render_template(
        "maintenance.html",
        maintenance_records=rows,
        counts=counts,
        block_content='dashboard'
    )


@app.route("/trucks/add", methods=["GET", "POST"])
@admin_required
def add_truck():
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    if request.method == "POST":
        if not _valid_csrf_token():
            flash("Your session token is invalid. Reload the page and try again.", "error")
            return redirect(url_for("trucks"))
        reg = request.form.get("registration_number")
        model = request.form.get("model")
        year = request.form.get("year")
        capacity = request.form.get("capacity_tons")
        status = request.form.get("status")
        last_service = request.form.get("last_service_date")

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO trucks (registration_number, model, year, capacity_tons, status, "
                "last_service_date) VALUES (?, ?, ?, ?, ?, ?)",
                (reg, model, year, capacity, status, last_service)
            )
            _record_user_activity(
                conn,
                "Truck Added",
                f"Added truck {reg}."
            )
            conn.commit()
            flash(f"Truck {reg} added successfully!", "success")
        except Exception as e:
            print(f"Error adding truck: {e}")
            flash("Error adding truck.", "error")
        finally:
            conn.close()
        return redirect(url_for("trucks"))

    conn = get_db()
    conn.close()
    return render_template(
        "truck_form.html",
        truck=None,
        csrf_token=session["csrf_token"],
        block_content="dashboard"
    )


@app.route("/trucks/<int:id>/edit", methods=["GET", "POST"])
@admin_required
def edit_truck(id):
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    conn = get_db()
    truck = conn.execute(
        "SELECT * FROM trucks WHERE truck_id = ?",
        (id,)
    ).fetchone()
    if not truck:
        conn.close()
        return "Truck not found.", 404

    if request.method == "POST":
        if not _valid_csrf_token():
            conn.close()
            flash("Your session token is invalid. Reload the page and try again.", "error")
            return redirect(url_for("edit_truck", id=id))
        reg = request.form.get("registration_number")
        model = request.form.get("model")
        year = request.form.get("year")
        capacity = request.form.get("capacity_tons")
        status = request.form.get("status")
        last_service = request.form.get("last_service_date")
        try:
            conn.execute(
                "UPDATE trucks SET registration_number=?, model=?, year=?, capacity_tons=?, "
                "status=?, last_service_date=? WHERE truck_id=?",
                (reg, model, year, capacity, status, last_service, id)
            )
            _record_user_activity(
                conn,
                "Truck Updated",
                f"Updated truck {reg}."
            )
            conn.commit()
            flash("Truck updated successfully!", "success")
        except Exception as e:
            print(f"Error editing truck: {e}")
            flash("Error updating truck.", "error")
        finally:
            conn.close()
        return redirect(url_for("trucks"))

    conn.close()
    return render_template(
        "truck_form.html",
        truck=truck,
        csrf_token=session["csrf_token"],
        block_content="dashboard"
    )


@app.route("/trucks/<int:id>/delete", methods=["POST"])
@admin_required
def delete_truck(id):
    if not _valid_csrf_token():
        flash("Your session token is invalid. Reload the page and try again.", "error")
        return redirect(url_for("trucks"))
    conn = get_db()
    try:
        truck = conn.execute(
            "SELECT registration_number FROM trucks WHERE truck_id = ?",
            (id,)
        ).fetchone()
        conn.execute(
            "DELETE FROM trucks WHERE truck_id = ?",
            (id,)
        )
        if truck:
            _record_user_activity(
                conn,
                "Truck Deleted",
                f"Deleted truck {truck['registration_number']}."
            )
        conn.commit()
        flash("Truck deleted successfully!", "success")
    except Exception as e:
        print(f"Error deleting truck: {e}")
        flash("Error deleting truck.", "error")
    finally:
        conn.close()
    return redirect(url_for("trucks"))


# ------------------------------------------------------------------ #
# Admin Module                                                       #
# ------------------------------------------------------------------ #

@app.route("/admin")
@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    conn = get_db()
    admin_notifications = _load_admin_notifications(conn)
    user_counts = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN is_approved = 1 AND is_rejected = 0 AND is_terminated = 0 THEN 1 ELSE 0 END) AS approved, "
        "SUM(CASE WHEN is_approved = 0 AND is_rejected = 0 AND is_terminated = 0 THEN 1 ELSE 0 END) AS pending, "
        "SUM(CASE WHEN is_rejected = 1 AND is_terminated = 0 THEN 1 ELSE 0 END) AS rejected, "
        "SUM(CASE WHEN is_terminated = 1 THEN 1 ELSE 0 END) AS terminated "
        "FROM users"
    ).fetchone()
    fleet_counts = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status = 'Available' THEN 1 ELSE 0 END) AS available, "
        "SUM(CASE WHEN status = 'In Trip' THEN 1 ELSE 0 END) AS in_trip, "
        "SUM(CASE WHEN status = 'Maintenance' THEN 1 ELSE 0 END) AS maintenance "
        "FROM trucks"
    ).fetchone()
    finance_summary, trip_finances = _get_trip_financials(
        conn,
        None if session.get("email", "").lower() == SUPER_ADMIN_EMAIL.lower() else "scoped"
    )
    activity_scope = (
        "" if session.get("email", "").lower() == SUPER_ADMIN_EMAIL.lower()
        else "WHERE subject.role = 'User' "
    )
    recent_activity = conn.execute(
        "SELECT logs.action, logs.details, logs.timestamp, "
        "subject.full_name AS subject_name, subject.email AS subject_email, "
        "actor.full_name AS actor_name, actor.email AS actor_email "
        "FROM user_logs AS logs "
        "LEFT JOIN users AS subject ON subject.user_id = logs.user_id "
        "LEFT JOIN users AS actor ON actor.user_id = logs.actor_user_id "
        f"{activity_scope}"
        "ORDER BY logs.timestamp DESC, logs.log_id DESC LIMIT 8"
    ).fetchall()
    challan_scope, challan_scope_params = _record_owner_filter("challans")
    challan_alerts = conn.execute(
        "SELECT challans.challan_id, challans.challan_number, challans.violation, "
        "challans.due_date, challans.amount, trucks.registration_number, "
        "CASE WHEN challans.due_date < date('now') THEN 'Overdue' "
        "WHEN challans.due_date = date('now') THEN 'Due today' ELSE 'Due soon' END AS due_label "
        "FROM challans JOIN trucks ON trucks.truck_id = challans.truck_id "
        f"WHERE {challan_scope} AND challans.status = 'Pending' "
        "AND challans.due_date IS NOT NULL "
        "AND challans.due_date <= date('now', '+30 days') "
        "ORDER BY challans.due_date ASC, challans.challan_id DESC LIMIT 8",
        challan_scope_params
    ).fetchall()
    challan_alert_summary = conn.execute(
        "SELECT SUM(CASE WHEN status = 'Pending' AND due_date < date('now') THEN 1 ELSE 0 END) AS overdue, "
        "SUM(CASE WHEN status = 'Pending' AND due_date >= date('now') "
        "AND due_date <= date('now', '+30 days') THEN 1 ELSE 0 END) AS due_soon, "
        "COALESCE(SUM(CASE WHEN status = 'Pending' THEN amount ELSE 0 END), 0) AS pending_amount "
        f"FROM challans WHERE {challan_scope}",
        challan_scope_params
    ).fetchone()
    conn.close()
    return render_template(
        "admin_dashboard.html",
        user_counts=user_counts,
        fleet_counts=fleet_counts,
        finance_summary=finance_summary,
        trip_finances=trip_finances[:10],
        recent_activity=recent_activity,
        admin_notifications=admin_notifications,
        challan_alerts=challan_alerts,
        challan_alert_summary=challan_alert_summary,
        block_content='dashboard'
    )


def _load_admin_notifications(conn):
    pending_users = conn.execute(
        "SELECT user_id, full_name, email, requested_role, created_at "
        "FROM users WHERE is_approved = 0 AND is_rejected = 0 AND is_terminated = 0 "
        "ORDER BY created_at DESC, user_id DESC LIMIT 50"
    ).fetchall()
    expense_scope, expense_scope_params = _record_owner_filter("trips")
    pending_expenses = conn.execute(
        "SELECT expenses.expense_id, expenses.trip_id, expenses.category, "
        "expenses.amount, expenses.created_at, users.full_name, users.email, "
        "trips.route_from, trips.route_to "
        "FROM expenses "
        "LEFT JOIN users ON users.user_id = expenses.created_by "
        "LEFT JOIN trips ON trips.trip_id = expenses.trip_id "
        "WHERE expenses.status = 'Pending' AND " + expense_scope + " "
        "ORDER BY expenses.created_at DESC, expenses.expense_id DESC LIMIT 50"
        , expense_scope_params
    ).fetchall()
    reset_links = conn.execute(
        "SELECT password_reset_tokens.reset_id, users.user_id, "
        "password_reset_tokens.admin_reset_url, "
        "password_reset_tokens.expires_at, password_reset_tokens.created_at, "
        "users.full_name, users.email "
        "FROM password_reset_tokens "
        "JOIN users ON users.user_id = password_reset_tokens.user_id "
        "WHERE users.role = 'User' "
        "AND users.is_approved = 1 "
        "AND users.is_rejected = 0 AND users.is_terminated = 0 "
        "AND password_reset_tokens.requested_by IS NULL "
        "AND password_reset_tokens.delivery_status = 'admin_queue' "
        "AND password_reset_tokens.used_at IS NULL "
        "AND password_reset_tokens.expires_at > CURRENT_TIMESTAMP "
        "ORDER BY password_reset_tokens.created_at DESC LIMIT 50"
    ).fetchall()
    compliance_scope, compliance_scope_params = _record_owner_filter("compliance_documents")
    compliance_expiry_alerts = conn.execute(
        "SELECT d.document_id, d.subject_type, d.document_type, d.document_number, "
        "d.expiry_date, d.verified, t.registration_number, dr.name AS driver_name, "
        "tr.route_from, tr.route_to, tr.trip_id AS related_trip_id, "
        "CASE WHEN d.expiry_date < date('now') THEN 'Expired' "
        "WHEN d.expiry_date = date('now') THEN 'Expires today' "
        "WHEN d.expiry_date <= date('now', '+7 days') THEN 'Expires within 7 days' "
        "ELSE 'Expires within 30 days' END AS alert_level, "
        "CAST(julianday(d.expiry_date) - julianday(date('now')) AS INTEGER) AS days_remaining "
        "FROM compliance_documents d "
        "LEFT JOIN trucks t ON t.truck_id = d.truck_id "
        "LEFT JOIN drivers dr ON dr.driver_id = d.driver_id "
        "LEFT JOIN trips tr ON tr.trip_id = d.trip_id "
        "WHERE " + compliance_scope.replace("compliance_documents.", "d.") + " "
        "AND d.expiry_date IS NOT NULL "
        "AND d.expiry_date <= date('now', '+30 days') "
        "AND d.document_id = (SELECT MAX(d2.document_id) FROM compliance_documents d2 "
        "WHERE d2.subject_type = d.subject_type AND d2.document_type = d.document_type "
        "AND IFNULL(d2.truck_id, 0) = IFNULL(d.truck_id, 0) "
        "AND IFNULL(d2.driver_id, 0) = IFNULL(d.driver_id, 0) "
        "AND IFNULL(d2.trip_id, 0) = IFNULL(d.trip_id, 0)) "
        "ORDER BY d.expiry_date ASC, d.document_id DESC LIMIT 100",
        compliance_scope_params
    ).fetchall()
    return {
        "pending_users": pending_users,
        "pending_expenses": pending_expenses,
        "reset_links": reset_links,
        "compliance_expiry": compliance_expiry_alerts,
    }


def _count_admin_notifications(conn):
    expense_scope, expense_scope_params = _record_owner_filter("trips")
    compliance_scope, compliance_scope_params = _record_owner_filter("compliance_documents")
    counts = conn.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM users "
        "WHERE is_approved = 0 AND is_rejected = 0 AND is_terminated = 0) AS pending_users, "
        "(SELECT COUNT(*) FROM expenses JOIN trips ON trips.trip_id = expenses.trip_id "
        "WHERE expenses.status = 'Pending' AND " + expense_scope + ") AS pending_expenses, "
        "(SELECT COUNT(*) FROM password_reset_tokens "
        "JOIN users ON users.user_id = password_reset_tokens.user_id "
        "WHERE users.role = 'User' AND users.is_approved = 1 "
        "AND users.is_rejected = 0 AND users.is_terminated = 0 "
        "AND password_reset_tokens.requested_by IS NULL "
        "AND password_reset_tokens.delivery_status = 'admin_queue' "
        "AND password_reset_tokens.used_at IS NULL "
        "AND password_reset_tokens.expires_at > CURRENT_TIMESTAMP) AS reset_links, "
        "(SELECT COUNT(*) FROM compliance_documents "
        "WHERE " + compliance_scope + " "
        "AND expiry_date IS NOT NULL "
        "AND expiry_date <= date('now', '+30 days') "
        "AND document_id = (SELECT MAX(d2.document_id) FROM compliance_documents d2 "
        "WHERE d2.subject_type = compliance_documents.subject_type "
        "AND d2.document_type = compliance_documents.document_type "
        "AND IFNULL(d2.truck_id, 0) = IFNULL(compliance_documents.truck_id, 0) "
        "AND IFNULL(d2.driver_id, 0) = IFNULL(compliance_documents.driver_id, 0) "
        "AND IFNULL(d2.trip_id, 0) = IFNULL(compliance_documents.trip_id, 0))) AS compliance_expiry"
        , [*expense_scope_params, *compliance_scope_params]
    ).fetchone()
    return {key: counts[key] for key in ("pending_users", "pending_expenses", "reset_links", "compliance_expiry")}


@app.context_processor
def inject_admin_notification_count():
    if not (session.get("is_admin") or session.get("role") == "Admin"):
        return {"admin_notification_count": 0}

    conn = get_db()
    try:
        counts = _count_admin_notifications(conn)
    finally:
        conn.close()
    return {"admin_notification_count": sum(counts.values())}


@app.route("/admin/notifications")
@admin_required
def admin_notifications():
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    conn = get_db()
    try:
        notifications = _load_admin_notifications(conn)
    finally:
        conn.close()
    return render_template(
        "admin_notifications.html",
        notifications=notifications,
        csrf_token=session["csrf_token"],
        block_content="dashboard"
    )


@app.route("/admin/notifications/count")
@admin_required
def admin_notification_count():
    conn = get_db()
    try:
        counts = _count_admin_notifications(conn)
    finally:
        conn.close()
    return jsonify(count=sum(counts.values()), breakdown=counts)


@app.route("/admin/audit")
@admin_required
def admin_audit():
    conn = get_db()
    activity_scope = (
        "" if session.get("email", "").lower() == SUPER_ADMIN_EMAIL.lower()
        else "WHERE subject.role = 'User' "
    )
    activity = conn.execute(
        "SELECT logs.action, logs.details, logs.timestamp, "
        "subject.full_name AS subject_name, subject.email AS subject_email, "
        "actor.full_name AS actor_name, actor.email AS actor_email "
        "FROM user_logs AS logs "
        "LEFT JOIN users AS subject ON subject.user_id = logs.user_id "
        "LEFT JOIN users AS actor ON actor.user_id = logs.actor_user_id "
        f"{activity_scope}"
        "ORDER BY logs.timestamp DESC, logs.log_id DESC LIMIT 200"
    ).fetchall()
    conn.close()
    return render_template("admin_audit.html", activity=activity, block_content='dashboard')


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def create_driver_account():
    if not _valid_csrf_token():
        flash("Your session token is invalid. Reload the user list and try again.", "error")
        return redirect(url_for("admin_users"))

    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    raw_driver_id = request.form.get("driver_id", "").strip()
    if not full_name or not email or not password:
        flash("Name, email, password, and driver record are required.", "error")
        return redirect(url_for("admin_users"))
    if (
        len(full_name) > 120
        or len(email) > 254
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
    ):
        flash("Enter a valid name and email address.", "error")
        return redirect(url_for("admin_users"))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("admin_users"))
    if len(password) > 128:
        flash("Password must be 128 characters or fewer.", "error")
        return redirect(url_for("admin_users"))
    if not raw_driver_id.isdigit():
        flash("Select an active, unlinked driver record.", "error")
        return redirect(url_for("admin_users"))

    driver_id = int(raw_driver_id)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        driver = conn.execute(
            "SELECT name FROM drivers WHERE driver_id = ? "
            "AND is_terminated = 0 AND status = 'Active'",
            (driver_id,)
        ).fetchone()
        linked_user = conn.execute(
            "SELECT user_id FROM users WHERE driver_id = ?",
            (driver_id,)
        ).fetchone()
        if not driver or linked_user:
            conn.rollback()
            flash("Choose an active driver who is not linked to another account.", "error")
            return redirect(url_for("admin_users"))

        cursor = conn.execute(
            "INSERT INTO users "
            "(full_name, email, password, is_approved, is_rejected, is_terminated, "
            "role, job_role, requested_role, driver_id) "
            "VALUES (?, ?, ?, 1, 0, 0, 'User', 'User', 'User', ?)",
            (full_name, email, generate_password_hash(password), driver_id)
        )
        user_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO user_logs (user_id, action, details, actor_user_id) "
            "VALUES (?, 'Driver Account Created', ?, ?)",
            (
                user_id,
                f"Driver account created and linked to driver record #{driver_id}.",
                session["user_id"]
            )
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        flash("That email address is already registered. Use a different email.", "error")
        return redirect(url_for("admin_users"))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    flash(f"Driver account created for {driver['name']}.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users")
@admin_required
def admin_users():
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    conn = get_db()
    rows = conn.execute(
        "SELECT users.user_id, users.full_name, users.email, users.created_at, "
        "users.is_approved, users.is_rejected, "
        "users.is_terminated, users.terminated_at, users.role, users.job_role, "
        "users.requested_role, users.driver_id, drivers.name AS driver_name "
        "FROM users LEFT JOIN drivers ON drivers.driver_id = users.driver_id "
        "ORDER BY users.created_at DESC, users.user_id DESC"
    ).fetchall()
    users = [dict(row) for row in rows]
    drivers = conn.execute(
        "SELECT drivers.driver_id, drivers.name, drivers.license_number, "
        "drivers.status, users.user_id AS linked_user_id "
        "FROM drivers LEFT JOIN users ON users.driver_id = drivers.driver_id "
        "WHERE (drivers.is_terminated = 0 AND drivers.status = 'Active') "
        "OR users.user_id IS NOT NULL "
        "ORDER BY drivers.name COLLATE NOCASE"
    ).fetchall()
    available_driver_records = [
        dict(driver) for driver in drivers
        if not driver["linked_user_id"] and driver["status"] == "Active"
    ]
    reset_requests = conn.execute(
        "SELECT users.user_id, users.full_name, users.email, "
        "MAX(password_reset_tokens.created_at) AS requested_at "
        "FROM password_reset_tokens "
        "JOIN users ON users.user_id = password_reset_tokens.user_id "
        "WHERE users.role = 'User' AND users.is_approved = 1 "
        "AND users.is_rejected = 0 AND users.is_terminated = 0 "
        "AND password_reset_tokens.requested_by IS NULL "
        "AND password_reset_tokens.delivery_status = 'admin_queue' "
        "AND password_reset_tokens.used_at IS NULL "
        "AND password_reset_tokens.expires_at > CURRENT_TIMESTAMP "
        "GROUP BY users.user_id, users.full_name, users.email "
        "ORDER BY requested_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return render_template(
        "admin_users.html",
        users=users,
        drivers=drivers,
        available_driver_records=available_driver_records,
        reset_requests=reset_requests,
        current_user_id=session['user_id'],
        super_admin_email=SUPER_ADMIN_EMAIL,
        is_super_admin=(
            session.get("email", "").casefold() == SUPER_ADMIN_EMAIL.casefold()
        ),
        csrf_token=session["csrf_token"],
        block_content='dashboard'
    )


@app.route("/admin/users/<int:id>/password-reset", methods=["POST"])
@admin_required
def admin_user_password_reset(id):
    if not _valid_csrf_token():
        flash("Your session token is invalid. Reload the user list and try again.", "error")
        return redirect(url_for("admin_users"))

    conn = get_db()
    user = conn.execute(
        "SELECT user_id, email, full_name FROM users "
        "WHERE user_id = ? AND role = 'User' AND is_approved = 1 "
        "AND is_rejected = 0 AND is_terminated = 0",
        (id,)
    ).fetchone()
    if not user:
        conn.close()
        flash("Password reset is available only for active, approved user accounts.", "error")
        return redirect(url_for("admin_users"))
    try:
        _, delivered_by_email, reset_url = _issue_password_reset(
            conn,
            user,
            session["user_id"]
        )
    except Exception:
        conn.rollback()
        app.logger.exception("Administrator password reset email could not be sent.")
        flash("Could not send a password reset link. Check email configuration and try again.", "error")
    else:
        if delivered_by_email:
            flash(f"A one-time password reset link was sent to {user['email']}.", "success")
            return redirect(url_for("admin_users"))
        else:
            return render_template(
                "admin_reset_link.html",
                reset_url=reset_url,
                user_email=user["email"],
                csrf_token=session["csrf_token"],
                block_content="dashboard"
            )
    finally:
        if conn:
            conn.close()
    return redirect(url_for("admin_users"))


@app.route("/account", methods=["GET", "POST"])
def account():
    user_id = session.get('user_id')
    if user_id is None:
        return redirect(url_for("login"))
    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    conn = get_db()
    user = conn.execute(
        "SELECT users.user_id, users.full_name, users.email, users.role, users.job_role, "
        "users.driver_id, drivers.name AS driver_name, users.created_at, "
        "users.is_approved, users.is_rejected, users.is_terminated, users.password "
        "FROM users LEFT JOIN drivers ON drivers.driver_id = users.driver_id "
        "WHERE users.user_id = ?",
        (user_id,)
    ).fetchone()
    if not user:
        conn.close()
        session.clear()
        return redirect(url_for("login"))
    if user['is_terminated']:
        conn.close()
        session.clear()
        flash("This account has been terminated.", "error")
        return redirect(url_for("login"))

    error = None
    success = None
    if request.method == "POST":
        submitted_token = request.form.get("csrf_token", "")
        session_token = session.get("csrf_token", "")
        if not submitted_token or not secrets.compare_digest(submitted_token, session_token):
            error = "Your session token is invalid. Reload the account page and try again."

        action = request.form.get("action")
        if not error and action == "update_profile":
            full_name = request.form.get("full_name", "").strip()
            if not full_name:
                error = "Name cannot be empty."
            elif len(full_name) > 100:
                error = "Name must be 100 characters or fewer."
            else:
                conn.execute(
                    "UPDATE users SET full_name = ? WHERE user_id = ?",
                    (full_name, user_id)
                )
                _record_user_activity(conn, "Profile Updated", "Updated account display name.")
                conn.commit()
                session['full_name'] = full_name
                session['avatar_initials'] = _avatar_initials(full_name, user['email'])
                success = "Your profile has been updated."
        elif not error and action == "change_password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            if not _password_matches(user['password'], current_password):
                error = "Current password is incorrect."
            elif len(new_password) < 8:
                error = "New password must be at least 8 characters."
            elif new_password != confirm_password:
                error = "New password and confirmation do not match."
            else:
                conn.execute(
                    "UPDATE users SET password = ? WHERE user_id = ?",
                    (generate_password_hash(new_password), user_id)
                )
                _record_user_activity(conn, "Password Changed", "Changed account password.")
                conn.commit()
                success = "Your password has been updated."
        elif not error:
            error = "Choose a valid account action."

    profile = conn.execute(
        "SELECT users.user_id, users.full_name, users.email, users.role, users.job_role, "
        "users.driver_id, drivers.name AS driver_name, users.created_at, "
        "users.is_approved, users.is_rejected "
        "FROM users LEFT JOIN drivers ON drivers.driver_id = users.driver_id "
        "WHERE users.user_id = ?",
        (user_id,)
    ).fetchone()
    profile = dict(profile)
    recent_activity = conn.execute(
        "SELECT action, details, timestamp FROM user_logs "
        "WHERE user_id = ? OR actor_user_id = ? "
        "ORDER BY timestamp DESC, log_id DESC LIMIT 20",
        (user_id, user_id)
    ).fetchall()
    conn.close()
    return render_template(
        "account.html",
        user=profile,
        recent_activity=recent_activity,
        csrf_token=session["csrf_token"],
        error=error,
        success=success,
        block_content='dashboard'
    )


@app.route("/admin/approve/<int:id>", methods=["GET", "POST"])
@admin_required
def approve_user(id):
    if request.method == "POST":
        if not _valid_csrf_token():
            flash("Your session token is invalid. Reload the user list and try again.", "error")
            return redirect(url_for("admin_users"))
        approved_role = request.form.get("approved_role", "User")
        if approved_role not in ("User", "Admin"):
            flash("Select a valid account access level.", "error")
            return redirect(url_for("admin_users"))
        if (
            approved_role == "Admin"
            and session.get("email", "").casefold() != SUPER_ADMIN_EMAIL.casefold()
        ):
            flash("Only the super administrator can approve administrator access.", "error")
            return redirect(url_for("admin_users"))
        driver_id = request.form.get("driver_id", "").strip()
        if approved_role == "User":
            if not driver_id.isdigit():
                flash("Select a driver before approving a Driver account.", "error")
                return redirect(url_for("admin_users"))
            driver_id = int(driver_id)
        else:
            driver_id = None
        conn = get_db()
        controller = UserAccessController(conn)
        try:
            approved = controller.approve_user(
                id,
                session['user_id'],
                approved_role,
                driver_id
            )
        except ValueError as error:
            conn.close()
            flash(str(error), "error")
            return redirect(url_for("admin_users"))
        conn.close()
        flash(
            f"User approved with {approved_role} access."
            if approved else "This account is no longer pending.",
            "success" if approved else "info"
        )
        return redirect(url_for("admin_users"))

    return redirect(url_for("admin_users"))


@app.route("/admin/reject/<int:id>", methods=["POST"])
@admin_required
def reject_user(id):
    if not _valid_csrf_token():
        flash("Your session token is invalid. Reload the user list and try again.", "error")
        return redirect(url_for("admin_users"))
    conn = get_db()
    controller = UserAccessController(conn)
    rejected = controller.reject_user(id, session['user_id'])
    conn.close()
    flash("User registration rejected." if rejected else "This account is no longer pending.", "success" if rejected else "info")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:id>/driver", methods=["POST"])
@admin_required
def assign_user_driver(id):
    if not _valid_csrf_token():
        flash("Your session token is invalid. Reload the user list and try again.", "error")
        return redirect(url_for("admin_users"))

    raw_driver_id = request.form.get("driver_id", "").strip()
    driver_id = int(raw_driver_id) if raw_driver_id.isdigit() else None
    if raw_driver_id and driver_id is None:
        flash("Select a valid driver.", "error")
        return redirect(url_for("admin_users"))

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute(
            "SELECT role, is_approved, is_rejected, is_terminated, driver_id "
            "FROM users WHERE user_id = ?",
            (id,)
        ).fetchone()
        if (
            not user
            or user["role"] != "User"
            or not user["is_approved"]
            or user["is_rejected"]
            or user["is_terminated"]
        ):
            conn.rollback()
            flash("Only active, approved Driver accounts can be linked.", "error")
            return redirect(url_for("admin_users"))

        if driver_id is not None:
            driver = conn.execute(
                "SELECT driver_id FROM drivers WHERE driver_id = ? "
                "AND is_terminated = 0 AND status = 'Active'",
                (driver_id,)
            ).fetchone()
            linked_user = conn.execute(
                "SELECT user_id FROM users WHERE driver_id = ? AND user_id != ?",
                (driver_id, id)
            ).fetchone()
            if not driver or linked_user:
                conn.rollback()
                flash("Choose an active driver who is not linked to another account.", "error")
                return redirect(url_for("admin_users"))

        active_trip = conn.execute(
            "SELECT trip_id FROM trips WHERE owner_user_id = ? "
            "AND status IN ('Scheduled', 'In Progress') LIMIT 1",
            (id,)
        ).fetchone()
        if active_trip:
            conn.rollback()
            flash(
                f"Driver assignment cannot change while Trip #{active_trip['trip_id']} is active.",
                "error"
            )
            return redirect(url_for("admin_users"))

        conn.execute("UPDATE users SET driver_id = ? WHERE user_id = ?", (driver_id, id))
        conn.execute(
            "INSERT INTO user_logs (user_id, action, details, actor_user_id) "
            "VALUES (?, 'Driver Account Linked', ?, ?)",
            (
                id,
                f"Driver account linked to driver record #{driver_id}."
                if driver_id else "Driver record unlinked from account.",
                session["user_id"]
            )
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        flash("That driver is already linked to another account.", "error")
        return redirect(url_for("admin_users"))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    flash("Driver account assignment saved.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:id>/role", methods=["POST"])
@admin_required
def update_user_job_role(id):
    if not _valid_csrf_token():
        flash("Your session token is invalid. Reload the user list and try again.", "error")
        return redirect(url_for("admin_users"))

    job_role = request.form.get("job_role", "")
    allowed_roles = {"User"}
    if job_role not in allowed_roles:
        flash("Select a valid operational role.", "error")
        return redirect(url_for("admin_users"))
    if id == session['user_id']:
        flash("You cannot change your own operational role.", "error")
        return redirect(url_for("admin_users"))

    conn = get_db()
    user = conn.execute(
        "SELECT email, job_role, is_approved, is_rejected, is_terminated, role "
        "FROM users WHERE user_id = ?",
        (id,)
    ).fetchone()
    if (
        not user
        or not user['is_approved']
        or user['is_rejected']
        or user['is_terminated']
        or user['role'] == 'Admin'
    ):
        conn.close()
        flash("Operational roles can only be assigned to active, approved user accounts.", "error")
        return redirect(url_for("admin_users"))

    conn.execute(
        "UPDATE users SET job_role = ? WHERE user_id = ?",
        (job_role, id)
    )
    conn.execute(
        "INSERT INTO user_logs (user_id, action, details, actor_user_id) "
        "VALUES (?, 'Operational Role Updated', ?, ?)",
        (id, f"Changed operational role from {user['job_role']} to {job_role}.", session['user_id'])
    )
    conn.commit()
    conn.close()
    flash(f"Operational role for {user['email']} updated to {job_role}.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/terminate/<int:id>", methods=["POST"])
@admin_required
def terminate_user(id):
    submitted_token = request.form.get("csrf_token", "")
    session_token = session.get("csrf_token", "")
    if not submitted_token or not secrets.compare_digest(submitted_token, session_token):
        flash("Your session token is invalid. Reload the user list and try again.", "error")
        return redirect(url_for("admin_users"))

    reason = request.form.get("reason", "").strip()
    if len(reason) < 5:
        flash("Enter a termination reason of at least 5 characters.", "error")
        return redirect(url_for("admin_users"))
    if len(reason) > 500:
        flash("Termination reason must be 500 characters or fewer.", "error")
        return redirect(url_for("admin_users"))

    conn = get_db()
    terminated = UserAccessController(conn).terminate_user(
        id,
        actor_user_id=session['user_id'],
        reason=reason
    )
    conn.close()
    flash(
        "Account terminated. Its records and audit history are retained."
        if terminated else "This account cannot be terminated or is already terminated.",
        "success" if terminated else "error"
    )
    return redirect(url_for("admin_users"))


# Register the enhanced expense screens after all original routes are defined.
from controller.expense_enhancements import register_expense_enhancements
register_expense_enhancements(app, globals())

# Register the full maintenance management workflow.
from controller.maintenance_enhancements import register_maintenance_module
register_maintenance_module(app, globals())

# Register traffic challan management and due-date alerts.
from controller.challan_enhancements import register_challan_module
register_challan_module(app, globals())

# Register roadside compliance documents and inspection workflows.
from controller.roadside_compliance import register_compliance_module
register_compliance_module(app, globals())


from controller.ai_assistant import assistant_bp
app.register_blueprint(assistant_bp)



@app.route("/transport-documents", methods=["GET", "POST"])
@access_required
def transport_documents():
    """Manage loading slips and POD references against existing trips."""
    if request.method == "POST":
        if not _valid_csrf_token():
            flash("Your session token is invalid. Reload this page and try again.", "error")
            return redirect(url_for("transport_documents"))

        action = request.form.get("action", "create").strip()
        conn = get_db()
        try:
            if action == "create":
                try:
                    trip_id = int(request.form.get("trip_id", ""))
                    document_date = request.form.get("document_date", "").strip()
                    datetime.strptime(document_date, "%Y-%m-%d")
                except (TypeError, ValueError):
                    flash("Choose a valid trip and document date.", "error")
                    return redirect(url_for("transport_documents"))

                document_type = request.form.get("document_type", "Loading Slip").strip()
                document_number = request.form.get("document_number", "").strip()
                cargo_description = request.form.get("cargo_description", "").strip()
                quantity = request.form.get("quantity", "").strip()
                notes = request.form.get("notes", "").strip()
                if document_type not in {"Loading Slip", "POD"}:
                    flash("Choose Loading Slip or POD.", "error")
                    return redirect(url_for("transport_documents"))
                if not document_number or len(document_number) > 80:
                    flash("Enter a document number up to 80 characters.", "error")
                    return redirect(url_for("transport_documents"))
                if len(cargo_description) > 300 or len(quantity) > 80 or len(notes) > 1000:
                    flash("Cargo, quantity, or notes exceed the allowed length.", "error")
                    return redirect(url_for("transport_documents"))

                trip_scope, trip_params = _record_owner_filter("trips")
                trip = conn.execute(
                    "SELECT trips.trip_id, trips.owner_user_id FROM trips "
                    f"WHERE trips.trip_id = ? AND trips.status != 'Cancelled' AND {trip_scope}",
                    [trip_id, *trip_params]
                ).fetchone()
                if not trip:
                    flash("Choose a trip you are allowed to access.", "error")
                    return redirect(url_for("transport_documents"))
                try:
                    cursor = conn.execute(
                        "INSERT INTO transport_documents "
                        "(trip_id, document_type, document_number, document_date, cargo_description, "
                        "quantity, status, pod_reference, notes, created_by) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            trip_id, document_type, document_number, document_date,
                            cargo_description, quantity,
                            "POD Received" if document_type == "POD" else "Draft",
                            document_number if document_type == "POD" else None,
                            notes, session["user_id"]
                        )
                    )
                except sqlite3.IntegrityError:
                    flash("That document number already exists for this trip and document type.", "error")
                    return redirect(url_for("transport_documents"))
                document_id = cursor.lastrowid
                conn.execute(
                    "INSERT INTO user_logs (user_id, action, details, actor_user_id) "
                    "VALUES (?, 'Transport Document Created', ?, ?)",
                    (
                        trip["owner_user_id"],
                        f"{document_type} {document_number} created for Trip #{trip_id}.",
                        session["user_id"]
                    )
                )
                conn.commit()
                flash(f"{document_type} {document_number} saved for Trip #{trip_id}.", "success")
                return redirect(url_for("transport_documents"))

            if action == "update_status":
                try:
                    document_id = int(request.form.get("document_id", ""))
                except (TypeError, ValueError):
                    flash("Choose a valid transport document.", "error")
                    return redirect(url_for("transport_documents"))
                new_status = request.form.get("status", "").strip()
                allowed_statuses = {
                    "Draft", "Ready for Loading", "Loaded", "In Transit",
                    "Delivered", "POD Received", "Cancelled"
                }
                if new_status not in allowed_statuses:
                    flash("Choose a valid document status.", "error")
                    return redirect(url_for("transport_documents"))
                pod_reference = request.form.get("pod_reference", "").strip()
                if len(pod_reference) > 120:
                    flash("POD reference must be 120 characters or fewer.", "error")
                    return redirect(url_for("transport_documents"))
                if new_status == "POD Received" and not pod_reference:
                    flash("Enter the POD reference before marking POD Received.", "error")
                    return redirect(url_for("transport_documents"))
                trip_scope, trip_params = _record_owner_filter("trips")
                existing = conn.execute(
                    "SELECT d.document_id, d.trip_id, d.document_type, d.document_number, "
                    "t.owner_user_id FROM transport_documents d JOIN trips t ON t.trip_id = d.trip_id "
                    f"WHERE d.document_id = ? AND {trip_scope}",
                    [document_id, *trip_params]
                ).fetchone()
                if not existing:
                    flash("Document not found or you do not have access.", "error")
                    return redirect(url_for("transport_documents"))
                conn.execute(
                    "UPDATE transport_documents SET status = ?, "
                    "pod_reference = CASE WHEN ? != '' THEN ? ELSE pod_reference END, "
                    "updated_at = CURRENT_TIMESTAMP WHERE document_id = ?",
                    (new_status, pod_reference, pod_reference, document_id)
                )
                conn.execute(
                    "INSERT INTO user_logs (user_id, action, details, actor_user_id) "
                    "VALUES (?, 'Transport Document Status Updated', ?, ?)",
                    (
                        existing["owner_user_id"],
                        f"{existing['document_type']} {existing['document_number']} "
                        f"for Trip #{existing['trip_id']} changed to {new_status}.",
                        session["user_id"]
                    )
                )
                conn.commit()
                flash("Transport document status updated.", "success")
                return redirect(url_for("transport_documents"))

            flash("Unsupported document action.", "error")
            return redirect(url_for("transport_documents"))
        finally:
            conn.close()

    conn = get_db()
    trip_scope, trip_params = _record_owner_filter("trips")
    trips = conn.execute(
        "SELECT trips.trip_id, trips.start_date, trips.route_from, trips.route_to, "
        "trucks.registration_number, drivers.name AS driver_name "
        "FROM trips LEFT JOIN trucks ON trucks.truck_id = trips.truck_id "
        "LEFT JOIN drivers ON drivers.driver_id = trips.driver_id "
        f"WHERE trips.status != 'Cancelled' AND {trip_scope} ORDER BY trips.trip_id DESC",
        trip_params
    ).fetchall()
    documents = conn.execute(
        "SELECT d.*, t.route_from, t.route_to, t.start_date, "
        "tr.registration_number, dr.name AS driver_name "
        "FROM transport_documents d JOIN trips t ON t.trip_id = d.trip_id "
        "LEFT JOIN trucks tr ON tr.truck_id = t.truck_id "
        "LEFT JOIN drivers dr ON dr.driver_id = t.driver_id "
        f"WHERE {trip_scope.replace('trips.', 't.')} "
        "ORDER BY d.updated_at DESC, d.document_id DESC LIMIT 500",
        trip_params
    ).fetchall()
    conn.close()
    return render_template(
        "transport_documents.html", trips=trips, documents=documents,
        csrf_token=session.get("csrf_token", ""), today=date.today().isoformat(), block_content="dashboard"
    )


@app.route("/reports", methods=["GET"])
@access_required
def reports():
    """Scoped operational and financial analytics with date filters."""
    today = date.today()
    default_start = date(today.year - 1, today.month, min(today.day, 28)).isoformat()
    start_date = request.args.get("start_date", default_start).strip()
    end_date = request.args.get("end_date", today.isoformat()).strip()
    try:
        start_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
        if start_obj > end_obj:
            raise ValueError("start date must not be after end date")
    except ValueError:
        flash("Choose a valid date range. The report has been reset to the default period.", "error")
        start_date, end_date = default_start, today.isoformat()

    conn = get_db()
    trip_scope, trip_params = _record_owner_filter("t")
    # _record_owner_filter applies driver assignment for non-admin accounts and
    # workspace-wide visibility for administrators.
    trip_where = f"{trip_scope} AND t.start_date BETWEEN ? AND ?"
    trip_args = [*trip_params, start_date, end_date]
    trip_summary = conn.execute(
        "SELECT COUNT(*) AS trip_count, "
        "COALESCE(SUM(CASE WHEN t.status = 'Completed' THEN 1 ELSE 0 END), 0) AS completed_count, "
        "COALESCE(SUM(CASE WHEN t.status = 'Cancelled' THEN 1 ELSE 0 END), 0) AS cancelled_count, "
        "COALESCE(SUM(CASE WHEN t.status IN ('Scheduled','In Progress') THEN 1 ELSE 0 END), 0) AS active_count, "
        "COALESCE(SUM(t.freight_amount), 0) AS freight_total "
        f"FROM trips t WHERE {trip_where}", trip_args
    ).fetchone()

    expense_scope, expense_params = _record_owner_filter("t")
    expense_where = (
        f"{expense_scope} AND e.date BETWEEN ? AND ? AND e.status = 'Approved' "
        "AND t.start_date BETWEEN ? AND ?"
    )
    expense_args = [*expense_params, start_date, end_date, start_date, end_date]
    expense_summary = conn.execute(
        "SELECT COALESCE(SUM(e.amount), 0) AS approved_expenses, COUNT(*) AS expense_count "
        "FROM expenses e JOIN trips t ON t.trip_id = e.trip_id WHERE " + expense_where,
        expense_args
    ).fetchone()

    payment_scope, payment_params = _record_owner_filter("t")
    payment_summary = conn.execute(
        "SELECT COALESCE(SUM(p.amount), 0) AS receipts, COUNT(*) AS payment_count "
        "FROM trip_payments p JOIN trips t ON t.trip_id = p.trip_id "
        f"WHERE {payment_scope} AND p.payment_date BETWEEN ? AND ? "
        "AND t.start_date BETWEEN ? AND ?",
        [*payment_params, start_date, end_date, start_date, end_date]
    ).fetchone()

    maintenance_scope, maintenance_params = _record_owner_filter("m")
    maintenance_summary = conn.execute(
        "SELECT COALESCE(SUM(m.cost), 0) AS maintenance_cost, COUNT(*) AS service_count "
        f"FROM maintenance m WHERE {maintenance_scope} AND m.service_date BETWEEN ? AND ?",
        [*maintenance_params, start_date, end_date]
    ).fetchone()
    monthly_maintenance = conn.execute(
        "SELECT strftime('%Y-%m', m.service_date) AS month, COALESCE(SUM(m.cost), 0) AS amount "
        f"FROM maintenance m WHERE {maintenance_scope} AND m.service_date BETWEEN ? AND ? "
        "GROUP BY month ORDER BY month",
        [*maintenance_params, start_date, end_date]
    ).fetchall()
    monthly_trips = conn.execute(
        "SELECT strftime('%Y-%m', t.start_date) AS month, COUNT(*) AS trips, "
        "COALESCE(SUM(t.freight_amount), 0) AS freight "
        f"FROM trips t WHERE {trip_where} GROUP BY month ORDER BY month",
        trip_args
    ).fetchall()
    monthly_expenses = conn.execute(
        "SELECT strftime('%Y-%m', e.date) AS month, COALESCE(SUM(e.amount), 0) AS amount "
        "FROM expenses e JOIN trips t ON t.trip_id = e.trip_id "
        f"WHERE {expense_where} GROUP BY month ORDER BY month",
        expense_args
    ).fetchall()
    monthly_payments = conn.execute(
        "SELECT strftime('%Y-%m', p.payment_date) AS month, COALESCE(SUM(p.amount), 0) AS amount "
        "FROM trip_payments p JOIN trips t ON t.trip_id = p.trip_id "
        f"WHERE {payment_scope} AND p.payment_date BETWEEN ? AND ? "
        "AND t.start_date BETWEEN ? AND ? GROUP BY month ORDER BY month",
        [*payment_params, start_date, end_date, start_date, end_date]
    ).fetchall()
    expense_categories = conn.execute(
        "SELECT COALESCE(e.category, 'Other') AS category, COALESCE(SUM(e.amount), 0) AS amount "
        "FROM expenses e JOIN trips t ON t.trip_id = e.trip_id "
        f"WHERE {expense_where} GROUP BY COALESCE(e.category, 'Other') ORDER BY amount DESC",
        expense_args
    ).fetchall()
    top_trips = conn.execute(
        "SELECT t.trip_id, t.route_from, t.route_to, t.start_date, t.status, "
        "COALESCE(t.freight_amount, 0) AS freight, "
        "COALESCE((SELECT SUM(p.amount) FROM trip_payments p WHERE p.trip_id=t.trip_id "
        "AND p.payment_date BETWEEN ? AND ?), 0) AS received, "
        "COALESCE((SELECT SUM(e.amount) FROM expenses e WHERE e.trip_id=t.trip_id "
        "AND e.status='Approved' AND e.date BETWEEN ? AND ?), 0) AS costs "
        f"FROM trips t WHERE {trip_where} ORDER BY t.start_date DESC, t.trip_id DESC LIMIT 10",
        [start_date, end_date, start_date, end_date, *trip_args]
    ).fetchall()
    conn.close()

    months = sorted(
        {row["month"] for row in monthly_trips}
        | {row["month"] for row in monthly_expenses}
        | {row["month"] for row in monthly_payments}
        | {row["month"] for row in monthly_maintenance}
    )
    month_labels = [datetime.strptime(m, "%Y-%m").strftime("%b %Y") for m in months]
    trip_by_month = {row["month"]: row for row in monthly_trips}
    expense_by_month = {row["month"]: float(row["amount"] or 0) for row in monthly_expenses}
    payment_by_month = {row["month"]: float(row["amount"] or 0) for row in monthly_payments}
    maintenance_by_month = {row["month"]: float(row["amount"] or 0) for row in monthly_maintenance}
    monthly_maintenance_values = [round(maintenance_by_month.get(m, 0), 2) for m in months]
    monthly_freight = [round(float(trip_by_month[m]["freight"] or 0), 2) if m in trip_by_month else 0 for m in months]
    monthly_expense_values = [round(expense_by_month.get(m, 0), 2) for m in months]
    monthly_payment_values = [round(payment_by_month.get(m, 0), 2) for m in months]
    category_labels = [row["category"] for row in expense_categories]
    category_values = [round(float(row["amount"] or 0), 2) for row in expense_categories]
    status_labels = ["Completed", "Scheduled / In Progress", "Cancelled"]
    status_values = [
        int(trip_summary["completed_count"] or 0),
        int(trip_summary["active_count"] or 0),
        int(trip_summary["cancelled_count"] or 0),
    ]
    freight_total = float(trip_summary["freight_total"] or 0)
    approved_expenses = float(expense_summary["approved_expenses"] or 0)
    receipts = float(payment_summary["receipts"] or 0)
    return render_template(
        "reports.html",
        start_date=start_date, end_date=end_date,
        trip_summary=trip_summary, expense_summary=expense_summary,
        payment_summary=payment_summary,
        maintenance_summary=maintenance_summary,
        monthly_maintenance=monthly_maintenance_values,
        freight_total=freight_total, approved_expenses=approved_expenses,
        receipts=receipts, estimated_trip_margin=freight_total-approved_expenses,
        collection_gap=freight_total-receipts,
        month_labels=month_labels, monthly_freight=monthly_freight,
        monthly_expenses=monthly_expense_values, monthly_payments=monthly_payment_values,
        category_labels=category_labels, category_values=category_values,
        status_labels=status_labels, status_values=status_values,
        top_trips=top_trips,
        block_content="dashboard",
    )


@app.route("/reports/export.csv", methods=["GET"])
@access_required
def export_reports_csv():
    """Download a scoped, date-filtered trip-level finance report."""
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    try:
        start_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
        if start_obj > end_obj:
            raise ValueError("start date must not be after end date")
    except ValueError:
        flash("Choose a valid date range before exporting the report.", "error")
        return redirect(url_for("reports"))

    conn = get_db()
    trip_scope, trip_params = _record_owner_filter("t")
    rows = conn.execute(
        "SELECT t.trip_id, t.start_date, t.route_from, t.route_to, t.status, "
        "COALESCE(tr.registration_number, '') AS truck, COALESCE(dr.name, '') AS driver, "
        "COALESCE(t.freight_amount, 0) AS freight, "
        "COALESCE((SELECT SUM(p.amount) FROM trip_payments p WHERE p.trip_id=t.trip_id "
        "AND p.payment_date BETWEEN ? AND ?), 0) AS received, "
        "COALESCE((SELECT SUM(e.amount) FROM expenses e WHERE e.trip_id=t.trip_id "
        "AND e.status='Approved' AND e.date BETWEEN ? AND ?), 0) AS approved_expenses "
        "FROM trips t LEFT JOIN trucks tr ON tr.truck_id=t.truck_id "
        "LEFT JOIN drivers dr ON dr.driver_id=t.driver_id "
        f"WHERE {trip_scope} AND t.start_date BETWEEN ? AND ? "
        "ORDER BY t.start_date DESC, t.trip_id DESC",
        [start_date, end_date, start_date, end_date, *trip_params, start_date, end_date]
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Trip ID", "Trip Date", "Origin", "Destination", "Status", "Truck", "Driver",
        "Booked Freight INR", "Collections INR", "Approved Trip Expenses INR",
        "Freight Less Approved Expenses INR"
    ])
    for row in rows:
        freight = float(row["freight"] or 0)
        costs = float(row["approved_expenses"] or 0)
        writer.writerow([
            row["trip_id"], row["start_date"], row["route_from"], row["route_to"],
            row["status"], row["truck"], row["driver"], f"{freight:.2f}",
            f"{float(row['received'] or 0):.2f}", f"{costs:.2f}", f"{freight-costs:.2f}"
        ])
    response_data = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    response_data.seek(0)
    return send_file(
        response_data, mimetype="text/csv", as_attachment=True,
        download_name=f"fleetflow-report-{start_date}-to-{end_date}.csv"
    )

if __name__ == "__main__":
    # Never enable the interactive debugger by default.
    app.run(debug=False, host=os.environ.get("FLASK_HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5001")))
