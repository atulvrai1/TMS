import os

from flask import session, redirect, url_for, flash
from controller.notification_service import NotificationService
from database.db import get_db, log_activity

# Configuration for the root of trust
SUPER_ADMIN_EMAIL = os.environ.get("FLEETFLOW_SUPER_ADMIN_EMAIL", "atulv.11239@gmail.com")

class UserAccessController:
    def __init__(self, db_connection):
        self.db = db_connection

    def is_authorized(self, user_id):
        """Checks if the user is approved and has not been rejected."""
        user = self.db.execute(
            "SELECT is_approved, is_rejected, is_terminated FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        return (
            user
            and user['is_approved'] == 1
            and not user['is_rejected']
            and not user['is_terminated']
        )

    def is_terminated(self, user_id):
        user = self.db.execute(
            "SELECT is_terminated FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return bool(user and user['is_terminated'])

    def is_super_admin(self, email):
        """Verifies if the user is the primary root admin."""
        return email == SUPER_ADMIN_EMAIL

    def is_admin(self, user_id):
        """Checks if the user has an Admin role."""
        user = self.db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return user and user['role'] == 'Admin'

    def approve_user(self, user_id, actor_user_id=None, role='User', driver_id=None):
        """Grants access to a user and assigns a role."""
        if role not in ('Admin', 'User'):
            raise ValueError("Approved account role must be Admin or User.")
        user = self.db.execute(
            "SELECT email, full_name FROM users "
            "WHERE user_id = ? AND is_approved = 0 AND is_rejected = 0 AND is_terminated = 0",
            (user_id,)
        ).fetchone()
        if not user:
            return False

        if role == 'User':
            if driver_id is None:
                raise ValueError("Assign an active driver before approving a Driver account.")
            driver = self.db.execute(
                "SELECT driver_id FROM drivers "
                "WHERE driver_id = ? AND is_terminated = 0 AND status = 'Active'",
                (driver_id,)
            ).fetchone()
            linked_user = self.db.execute(
                "SELECT user_id FROM users WHERE driver_id = ? AND user_id != ?",
                (driver_id, user_id)
            ).fetchone()
            if not driver or linked_user:
                raise ValueError("Choose an active driver who is not linked to another account.")
        else:
            driver_id = None

        self.db.execute(
            "UPDATE users SET is_approved = 1, is_rejected = 0, role = ?, driver_id = ? "
            "WHERE user_id = ?",
            (role, driver_id, user_id)
        )
        self.db.commit()

        NotificationService.send_email(
            recipient_email=user['email'],
            subject="Account Approved - FleetFlow",
            body=(
                f"Hello {user['full_name'] or 'User'},\n\n" +
                (
                    "Your Driver account has been approved. Sign in to view your assigned trips "
                    "and submit expenses for administrator review."
                    if role == 'User' else
                    "Your Administrator account has been approved. Sign in to manage the fleet."
                )
            )
        )

        log_activity(
            user_id,
            "Account Approved",
            f"User {user['email']} was approved with {role} access.",
            actor_user_id=actor_user_id
        )
        return True

    def reject_user(self, user_id, actor_user_id=None):
        """Rejects a pending account without deleting its registration."""
        user = self.db.execute(
            "SELECT email FROM users "
            "WHERE user_id = ? AND is_approved = 0 AND is_rejected = 0 AND is_terminated = 0",
            (user_id,)
        ).fetchone()
        if not user:
            return False

        self.db.execute(
            "UPDATE users SET is_rejected = 1 WHERE user_id = ?",
            (user_id,)
        )
        self.db.commit()
        log_activity(
            user_id,
            "Account Rejected",
            f"User {user['email']} was rejected.",
            actor_user_id=actor_user_id
        )
        return True

    def terminate_user(self, user_id, actor_user_id, reason):
        """Disable an account while retaining its user and activity records."""
        user = self.db.execute(
            "SELECT email FROM users "
            "WHERE user_id = ? AND is_terminated = 0",
            (user_id,)
        ).fetchone()
        if not user or user_id == actor_user_id or self.is_super_admin(user['email']):
            return False

        self.db.execute(
            "UPDATE users SET is_terminated = 1, terminated_at = CURRENT_TIMESTAMP "
            "WHERE user_id = ?",
            (user_id,)
        )
        self.db.execute(
            "INSERT INTO user_logs (user_id, action, details, actor_user_id) "
            "VALUES (?, ?, ?, ?)",
            (
                user_id,
                "Account Terminated",
                f"Account {user['email']} was terminated. Reason: {reason}",
                actor_user_id
            )
        )
        self.db.commit()
        return True

def access_required(f):
    """Decorator to restrict access to approved, logged-in users."""
    from functools import wraps
    from flask import request

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        from database.db import get_db
        conn = get_db()
        controller = UserAccessController(conn)
        authorized = controller.is_authorized(session['user_id'])
        terminated = controller.is_terminated(session['user_id'])
        user = conn.execute(
            "SELECT email, role, job_role, driver_id FROM users WHERE user_id = ?",
            (session['user_id'],)
        ).fetchone()
        conn.close()

        if not authorized:
            if terminated:
                session.clear()
                return redirect(url_for('login'))
            return redirect(url_for('pending_approval'))

        session['email'] = user['email']
        session['role'] = user['role']
        session['job_role'] = user['job_role']
        session['driver_id'] = user['driver_id']
        session['is_admin'] = (
            user['role'] == 'Admin'
            or controller.is_super_admin(user['email'])
        )
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Decorator to restrict access to Admins only."""
    from functools import wraps

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        from database.db import get_db
        conn = get_db()
        controller = UserAccessController(conn)
        is_authorized = controller.is_authorized(session['user_id'])
        # Read the identity and role from the database. Session values may be
        # stale after an account change and must not grant administrator access.
        user = conn.execute(
            "SELECT email, role FROM users WHERE user_id = ?",
            (session['user_id'],)
        ).fetchone()
        is_admin = bool(
            is_authorized
            and user
            and (
                user['role'] == 'Admin'
                or controller.is_super_admin(user['email'])
            )
        )
        conn.close()

        if not is_admin:
            if not is_authorized:
                session.clear()
                return redirect(url_for('login'))
            return "Access Denied: Administrator privileges required.", 403

        session['is_admin'] = True
        return f(*args, **kwargs)
    return decorated_function
