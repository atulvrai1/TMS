import sqlite3
import os

DATABASE_PATH = os.environ.get("DATABASE_PATH", "tms.db")
SUPER_ADMIN_EMAIL = os.environ.get("FLEETFLOW_SUPER_ADMIN_EMAIL", "atulv.11239@gmail.com")

def get_db():
    """Returns a SQLite connection with row_factory and foreign keys enabled."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def log_activity(user_id, action, details, actor_user_id=None):
    """Record an auditable action against a user and its optional actor."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO user_logs (user_id, action, details, actor_user_id) VALUES (?, ?, ?, ?)",
            (user_id, action, details, actor_user_id)
        )
        conn.commit()
    finally:
        conn.close()

def init_db():
    """Creates all tables for the Truck Management System."""
    with get_db() as conn:
        # Users Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                full_name TEXT,
                is_approved BOOLEAN DEFAULT 0,
                is_rejected BOOLEAN NOT NULL DEFAULT 0,
                is_terminated BOOLEAN NOT NULL DEFAULT 0,
                terminated_at DATETIME,
                role TEXT CHECK(role IN ('Admin', 'User')) DEFAULT 'User',
                job_role TEXT NOT NULL DEFAULT 'User',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "full_name" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN full_name TEXT")
        if "is_rejected" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN is_rejected BOOLEAN NOT NULL DEFAULT 0")
        if "is_terminated" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN is_terminated BOOLEAN NOT NULL DEFAULT 0")
        if "terminated_at" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN terminated_at DATETIME")
        if "job_role" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN job_role TEXT NOT NULL DEFAULT 'User'")
        if "requested_role" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN requested_role TEXT NOT NULL DEFAULT 'User'")
        conn.execute('''
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                reset_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at DATETIME NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                used_at DATETIME,
                requested_by INTEGER,
                delivery_status TEXT NOT NULL DEFAULT 'unknown',
                admin_reset_url TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (requested_by) REFERENCES users(user_id)
            )
        ''')
        reset_token_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(password_reset_tokens)")
        }
        if "delivery_status" not in reset_token_columns:
            conn.execute(
                "ALTER TABLE password_reset_tokens "
                "ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "admin_reset_url" not in reset_token_columns:
            conn.execute(
                "ALTER TABLE password_reset_tokens ADD COLUMN admin_reset_url TEXT"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_password_reset_user "
            "ON password_reset_tokens(user_id, expires_at, used_at)"
        )

        # User Logs Table (Audit Trail)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                details TEXT,
                actor_user_id INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (actor_user_id) REFERENCES users(user_id)
            )
        ''')
        log_columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_logs)")}
        if "actor_user_id" not in log_columns:
            conn.execute("ALTER TABLE user_logs ADD COLUMN actor_user_id INTEGER REFERENCES users(user_id)")

        # Trucks Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS trucks (
                truck_id INTEGER PRIMARY KEY AUTOINCREMENT,
                registration_number TEXT NOT NULL UNIQUE,
                model TEXT NOT NULL,
                year INTEGER NOT NULL,
                capacity_tons REAL,
                status TEXT CHECK(status IN ('Available', 'In Trip', 'Maintenance')) DEFAULT 'Available',
                last_service_date DATE,
                owner_user_id INTEGER REFERENCES users(user_id)
            )
        ''')
        truck_columns = {row["name"] for row in conn.execute("PRAGMA table_info(trucks)")}
        if "owner_user_id" not in truck_columns:
            conn.execute("ALTER TABLE trucks ADD COLUMN owner_user_id INTEGER REFERENCES users(user_id)")

        # Drivers Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS drivers (
                driver_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                license_number TEXT NOT NULL UNIQUE,
                contact_info TEXT,
                status TEXT CHECK(status IN ('Active', 'On Leave')) DEFAULT 'Active',
                is_terminated BOOLEAN NOT NULL DEFAULT 0,
                terminated_at DATETIME,
                termination_reason TEXT
            )
        ''')
        driver_columns = {row["name"] for row in conn.execute("PRAGMA table_info(drivers)")}
        if "is_terminated" not in driver_columns:
            conn.execute(
                "ALTER TABLE drivers ADD COLUMN is_terminated BOOLEAN NOT NULL DEFAULT 0"
            )
        if "terminated_at" not in driver_columns:
            conn.execute("ALTER TABLE drivers ADD COLUMN terminated_at DATETIME")
        if "termination_reason" not in driver_columns:
            conn.execute("ALTER TABLE drivers ADD COLUMN termination_reason TEXT")
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "driver_id" not in user_columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN driver_id INTEGER REFERENCES drivers(driver_id)"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_driver "
            "ON users(driver_id) WHERE driver_id IS NOT NULL"
        )
        conn.execute(
            "UPDATE users SET job_role = 'User' WHERE role = 'User' AND job_role != 'User'"
        )
        if "owner_user_id" not in driver_columns:
            conn.execute("ALTER TABLE drivers ADD COLUMN owner_user_id INTEGER REFERENCES users(user_id)")
        conn.execute('''
            CREATE TABLE IF NOT EXISTS driver_documents (
                document_id INTEGER PRIMARY KEY AUTOINCREMENT,
                driver_id INTEGER NOT NULL,
                document_type TEXT NOT NULL,
                original_name TEXT NOT NULL,
                storage_name TEXT NOT NULL UNIQUE,
                file_size INTEGER NOT NULL,
                uploaded_by INTEGER,
                uploaded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (driver_id) REFERENCES drivers(driver_id) ON DELETE CASCADE,
                FOREIGN KEY (uploaded_by) REFERENCES users(user_id)
            )
        ''')
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_driver_documents_driver "
            "ON driver_documents(driver_id, uploaded_at DESC, document_id DESC)"
        )

        # Trips Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS trips (
                trip_id INTEGER PRIMARY KEY AUTOINCREMENT,
                truck_id INTEGER NOT NULL,
                driver_id INTEGER NOT NULL,
                freight_amount REAL NOT NULL DEFAULT 0,
                start_date DATE NOT NULL,
                end_date DATE,
                start_odometer INTEGER,
                end_odometer INTEGER,
                route_from TEXT NOT NULL,
                route_to TEXT NOT NULL,
                status TEXT CHECK(status IN ('Scheduled', 'In Progress', 'Completed', 'Cancelled')) DEFAULT 'Scheduled',
                owner_user_id INTEGER REFERENCES users(user_id),
                FOREIGN KEY (truck_id) REFERENCES trucks(truck_id),
                FOREIGN KEY (driver_id) REFERENCES drivers(driver_id)
            )
        ''')
        trip_columns = {row["name"] for row in conn.execute("PRAGMA table_info(trips)")}
        if "freight_amount" not in trip_columns:
            conn.execute(
                "ALTER TABLE trips ADD COLUMN freight_amount REAL NOT NULL DEFAULT 0"
            )
        if "owner_user_id" not in trip_columns:
            conn.execute("ALTER TABLE trips ADD COLUMN owner_user_id INTEGER REFERENCES users(user_id)")

        # Expenses Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS expenses (
                expense_id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id INTEGER NOT NULL,
                category TEXT CHECK(category IN (
                    'Fuel', 'Toll', 'Food', 'Maintenance', 'Meals/Allowance',
                    'Maintenance/Repairs', 'Loading/Unloading', 'Other'
                )),
                amount REAL NOT NULL,
                date DATE NOT NULL,
                description TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_by INTEGER,
                status TEXT NOT NULL DEFAULT 'Approved'
                    CHECK(status IN ('Pending', 'Approved', 'Rejected')),
                reviewed_by INTEGER,
                reviewed_at DATETIME,
                FOREIGN KEY (created_by) REFERENCES users(user_id),
                FOREIGN KEY (reviewed_by) REFERENCES users(user_id),
                FOREIGN KEY (trip_id) REFERENCES trips(trip_id)
            )
        ''')
        expense_columns = {row["name"] for row in conn.execute("PRAGMA table_info(expenses)")}
        if "created_by" not in expense_columns:
            conn.execute("ALTER TABLE expenses ADD COLUMN created_by INTEGER REFERENCES users(user_id)")
        if "status" not in expense_columns:
            conn.execute(
                "ALTER TABLE expenses ADD COLUMN status TEXT NOT NULL DEFAULT 'Approved'"
            )
        if "reviewed_by" not in expense_columns:
            conn.execute("ALTER TABLE expenses ADD COLUMN reviewed_by INTEGER REFERENCES users(user_id)")
        if "reviewed_at" not in expense_columns:
            conn.execute("ALTER TABLE expenses ADD COLUMN reviewed_at DATETIME")
        if "created_at" not in expense_columns:
            conn.execute("ALTER TABLE expenses ADD COLUMN created_at DATETIME")
        expense_schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'expenses'"
        ).fetchone()["sql"]
        if "Meals/Allowance" not in expense_schema:
            conn.execute('''
                CREATE TABLE expenses_new (
                    expense_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trip_id INTEGER NOT NULL,
                    category TEXT CHECK(category IN (
                        'Fuel', 'Toll', 'Food', 'Maintenance', 'Meals/Allowance',
                        'Maintenance/Repairs', 'Loading/Unloading', 'Other'
                    )),
                    amount REAL NOT NULL,
                    date DATE NOT NULL,
                    description TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    created_by INTEGER,
                    status TEXT NOT NULL DEFAULT 'Approved'
                        CHECK(status IN ('Pending', 'Approved', 'Rejected')),
                    reviewed_by INTEGER,
                    reviewed_at DATETIME,
                    FOREIGN KEY (created_by) REFERENCES users(user_id),
                    FOREIGN KEY (reviewed_by) REFERENCES users(user_id),
                    FOREIGN KEY (trip_id) REFERENCES trips(trip_id)
                )
            ''')
            conn.execute(
                "INSERT INTO expenses_new "
                "(expense_id, trip_id, category, amount, date, description, created_at, "
                "created_by, status, reviewed_by, reviewed_at) "
                "SELECT expense_id, trip_id, category, amount, date, description, created_at, "
                "created_by, status, reviewed_by, reviewed_at FROM expenses"
            )
            conn.execute("DROP TABLE expenses")
            conn.execute("ALTER TABLE expenses_new RENAME TO expenses")

        conn.execute('''
            CREATE TABLE IF NOT EXISTS trip_payments (
                payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id INTEGER NOT NULL,
                amount REAL NOT NULL CHECK(amount > 0),
                payment_date DATE NOT NULL,
                reference TEXT,
                recorded_by INTEGER,
                recorded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (trip_id) REFERENCES trips(trip_id),
                FOREIGN KEY (recorded_by) REFERENCES users(user_id)
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS financial_corrections (
                correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_type TEXT NOT NULL CHECK(record_type IN ('Expense', 'Payment')),
                record_id INTEGER NOT NULL,
                old_amount REAL NOT NULL,
                new_amount REAL NOT NULL,
                old_status TEXT,
                new_status TEXT,
                reason TEXT NOT NULL,
                changed_by INTEGER,
                changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (changed_by) REFERENCES users(user_id)
            )
        ''')
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_financial_corrections_record "
            "ON financial_corrections(record_type, record_id, correction_id DESC)"
        )

        # Maintenance Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS maintenance (
                maintenance_id INTEGER PRIMARY KEY AUTOINCREMENT,
                truck_id INTEGER NOT NULL,
                service_date DATE NOT NULL,
                service_type TEXT NOT NULL,
                cost REAL NOT NULL,
                notes TEXT,
                owner_user_id INTEGER REFERENCES users(user_id),
                FOREIGN KEY (truck_id) REFERENCES trucks(truck_id)
            )
        ''')
        maintenance_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(maintenance)")
        }
        if "owner_user_id" not in maintenance_columns:
            conn.execute("ALTER TABLE maintenance ADD COLUMN owner_user_id INTEGER REFERENCES users(user_id)")

        super_admin = conn.execute(
            "SELECT user_id FROM users WHERE lower(email) = lower(?)",
            (SUPER_ADMIN_EMAIL,)
        ).fetchone()
        if super_admin:
            super_admin_id = super_admin["user_id"]
            for table in ("trips", "maintenance"):
                conn.execute(
                    f"UPDATE {table} SET owner_user_id = ? WHERE owner_user_id IS NULL",
                    (super_admin_id,)
                )
        # Trucks and drivers are shared fleet resources, not user-owned records.
        conn.execute("UPDATE trucks SET owner_user_id = NULL WHERE owner_user_id IS NOT NULL")
        conn.execute("UPDATE drivers SET owner_user_id = NULL WHERE owner_user_id IS NOT NULL")
        if super_admin:
            super_admin_id = super_admin["user_id"]
            conn.execute(
                "UPDATE expenses SET created_by = ? WHERE created_by IS NULL",
                (super_admin_id,)
            )
            conn.execute(
                "UPDATE trip_payments SET recorded_by = ? WHERE recorded_by IS NULL",
                (super_admin_id,)
            )
            conn.execute(
                "UPDATE financial_corrections SET changed_by = ? WHERE changed_by IS NULL",
                (super_admin_id,)
            )
            conn.execute(
                "UPDATE driver_documents SET uploaded_by = ? WHERE uploaded_by IS NULL",
                (super_admin_id,)
            )

        for table in ("trucks", "drivers", "trips", "maintenance"):
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_owner "
                f"ON {table}(owner_user_id)"
            )
        conn.commit()

def seed_db():
    """Inserts sample data for development."""
    with get_db() as conn:
        # Seed Admin
        conn.execute(
            "INSERT OR IGNORE INTO users (email, password, full_name, is_approved, role) VALUES (?, ?, ?, ?, ?)",
            (SUPER_ADMIN_EMAIL, 'password123', 'Atul Admin', 1, 'Admin')
        )

        # Seed Trucks
        trucks = [
            ('KA-01-AB-1234', 'Tata Prima', 2022, 25.0, 'Available', '2026-01-10'),
            ('KA-02-XY-5678', 'Ashok Leyland AVTR', 2021, 20.0, 'Available', '2026-03-15'),
            ('KA-03-ZZ-9999', 'BharatBenz', 2023, 30.0, 'Maintenance', '2026-10-01'),
        ]
        conn.executemany(
            'INSERT OR IGNORE INTO trucks '
            '(registration_number, model, year, capacity_tons, status, last_service_date) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            trucks
        )

        # Seed Drivers
        drivers = [
            ('Rajesh Kumar', 'DL-1234567', '9876543210', 'Active'),
            ('Suresh Raina', 'DL-7654321', '9876543211', 'Active'),
            ('Amit Singh', 'DL-1122334', '9876543212', 'On Leave'),
        ]
        conn.executemany(
            'INSERT OR IGNORE INTO drivers '
            '(name, license_number, contact_info, status) VALUES (?, ?, ?, ?)',
            drivers
        )
        conn.commit()

if __name__ == "__main__":
    init_db()
    seed_db()
    print("Database initialized and seeded successfully.")
