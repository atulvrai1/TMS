# FleetFlow — Truck Management System

FleetFlow is a Flask and SQLite web application for managing trucks, drivers, trips, maintenance, expenses, payments, account approval, and audit history.

## Requirements

- Python 3.10 or newer
- pip
- A mail provider only if password-reset emails should be sent through SMTP

## Local setup

From the repository root:

```bash
cd expense-tracker
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set a unique, random `FLASK_SECRET_KEY` of at least 32 characters. Generate one with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Set `FLEETFLOW_BOOTSTRAP_ADMIN_EMAIL` and `FLEETFLOW_BOOTSTRAP_ADMIN_PASSWORD` only when intentionally bootstrapping an administrator in a **new development database**. The password must be at least 12 characters. Existing databases are not automatically reset. Never use a shared/default password.

Run the development server:

```bash
python app.py
```

The development server binds to `127.0.0.1:5001` by default. Do not expose Flask's development server to the public internet.

## Docker setup

Docker Compose runs FleetFlow with Gunicorn and keeps the SQLite database and uploaded driver documents in persistent named volumes.

Requirements: Docker Engine and the Docker Compose plugin.

From the repository root:

```bash
cp expense-tracker/.env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Edit the root `.env` file and replace `FLASK_SECRET_KEY` with the generated value. Keep this file private; it is ignored by Git. Do not use a shared or default secret.

Build and start the application:

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f fleetflow
```

Open [http://localhost:5001](http://localhost:5001).

Useful commands:

```bash
docker compose logs -f fleetflow  # Follow application logs
docker compose restart fleetflow  # Restart the app
docker compose down               # Stop/remove container; keep persistent data
docker compose build --no-cache   # Rebuild the image without build cache
```

The `fleetflow_data` volume stores the SQLite database; `fleetflow_instance` stores instance files, including uploaded driver documents. **Do not run `docker compose down -v` unless you intentionally want to delete those volumes and their data.** Back up important production data separately.

GitHub Actions builds the Docker image and runs a container smoke test on relevant pushes and pull requests. It does not publish the image to a registry.

## Tests

```bash
cd expense-tracker
source .venv/bin/activate
pytest -q
```

Tests use a temporary database and should not change your working database.

## Main workflows and usability

- **Dispatch lifecycle:** schedule a trip with an available truck and active driver, dispatch it with a starting odometer, and complete it with a validated return odometer. Truck availability is updated with the trip transition.
- **Customer payments:** record dated payments and receipt/reference notes against a trip; amounts above the outstanding freight balance are rejected.
- **Expenses and review:** link expenses to trips and review their approval state; approved costs feed the profitability summaries.
- **Fleet and access:** manage trucks, drivers, maintenance records, user approvals, and audit history according to account permissions.
- **Operator experience:** responsive dashboard navigation, accessible keyboard focus, table quick-search, mobile-friendly horizontal table scrolling, and protection against accidental duplicate trip/payment submissions.

The automated trip workflow test covers scheduling, dispatch, odometer validation, completion, truck availability, and payment balance enforcement against a temporary database. These checks are useful regression coverage, not a substitute for testing with your real operational data and deployment configuration.

## Production deployment

1. Store secrets in your deployment platform's secret manager/environment; do not commit `.env`.
2. Set `APP_ENV=production`, a unique `FLASK_SECRET_KEY`, and a correct `FLEETFLOW_PUBLIC_URL`.
3. Configure `DATABASE_PATH` to an absolute path on persistent storage with restricted filesystem permissions.
4. Serve through HTTPS behind a reverse proxy and use a production WSGI server, e.g. Gunicorn:

   ```bash
   gunicorn --workers 2 --bind 127.0.0.1:8000 app:app
   ```

5. Back up the database and private document directory regularly; test restore procedures.
6. Configure SMTP environment variables if password-reset emails are required.
7. Monitor application logs, disk space, database backups, and failed email delivery.

SQLite is suitable for local development and small deployments. Evaluate PostgreSQL before scaling to multiple application instances or heavier concurrent writes.

## Data and credential safety

- Do not commit `.env`, `.flask_secret_key`, database files, private uploads, or email logs.
- If a secret was ever committed, rotate it immediately. Deleting the latest copy does not remove it from Git history.
- Before removing an existing database from version control, take a verified backup and confirm it does not contain the only copy of operational records.
- Never share password-reset links or private driver documents in logs or support tickets.

## Deployment note

Legacy accounts with plaintext passwords are intentionally not accepted by the updated login logic. Reset those passwords through the supported recovery/admin process before deploying this change. Back up and verify your database before deployment.
