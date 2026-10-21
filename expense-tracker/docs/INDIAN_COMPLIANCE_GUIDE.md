# FleetFlow Indian Vehicle Compliance — Quick Guide

## What this module does

FleetFlow's **Roadside Compliance** page is an internal register for document dates, reminder statuses, and roadside inspection notes. It does not itself verify government records, determine every legal requirement, or replace official portals.

Supported records include:

- Truck: Registration Certificate (RC), PUC Certificate, Third-Party Insurance, Fitness Certificate, Commercial Permit, National Permit, State Permit, and Road Tax / Tax Token.
- Driver: Driving Licence (HMV).
- Trip: Trip Sheet / E-Way Bill / Cargo Manifest.

The page also records roadside inspection findings (documents, weight/cargo, vehicle safety, driver behaviour, and operational rules) and lets an admin track corrective actions.

## How to use it

1. Sign in with an **Admin** account.
2. Open **Roadside Compliance** in the FleetFlow sidebar.
3. Under **Register a compliance document**, choose whether the record belongs to a truck, driver, or trip.
4. Choose the document type and the relevant truck/driver/trip.
5. Enter the document number/reference, issue date, expiry date, verification method, and notes. Mark it verified only after an authorised person has checked the document.
6. Select **Save document record**. The register shows its current expiry status.
7. Repeat for each truck and driver. Add a new record after renewal so the newest record reflects the renewed validity.
8. Review **Pre-trip document check (advisory)** before dispatch. It flags missing records, missing expiry dates, expired records, and records approaching expiry for PUC, insurance, fitness, and permits.
9. Use **Record roadside inspection** to log observed findings, severity, official challan reference (if one exists), and corrective action. Update follow-up status when resolved.

## Reminder/status windows

- **Expired:** expiry date has passed.
- **Due today:** expires today.
- **Urgent:** expires in 1–7 days.
- **Expiring soon:** expires in 8–30 days.
- **Valid:** more than 30 days remain.
- **Missing record / Expiry date missing:** an admin needs to complete the register.

These are FleetFlow operational alert windows, not statutory definitions or a claim that a vehicle is legally cleared.

## Official government links

The page opens these links in a new tab (it does not embed them in a frame):

- Parivahan vehicle-status report (includes vehicle status, PUCC/insurance status and pending challans where available): https://analytics.parivahan.gov.in/analytics/vehiclestatusreport
- VAHAN PUC service: https://vahan.parivahan.gov.in/puc/views/PucCertificate.xhtml
- Official eChallan portal: https://echallan.parivahan.gov.in/
- Parivahan main portal: https://parivahan.gov.in/

Open the official service, complete any CAPTCHA/verification there, and manually update FleetFlow after checking the result. This release does **not** automatically query Parivahan, bypass CAPTCHA, or send data to a government service. Automatic verification requires an authorised API or approved provider.

## Automatic sync: what is required

FleetFlow now has an authorised-provider adapter, but it **does not automatically connect to the public Parivahan web pages**. Those pages can present CAPTCHA and citizen-verification steps. The official National Transport Repository data-sharing policy describes a formal API request/approval process; obtain written authorisation or use an approved provider before configuring a live endpoint.

When an authorised provider is available, configure these variables in the application's runtime environment (do not commit tokens to Git):

- `PARIVAHAN_API_BASE_URL`: HTTPS base URL for the authorised API adapter, not a public citizen-portal URL.
- `PARIVAHAN_API_TOKEN`: bearer token supplied by that provider.

FleetFlow expects a provider contract with `GET {base_url}/vehicles/{URL-encoded-registration-number}`, an `Authorization: Bearer ...` header, and JSON shaped like:

```json
{
  "source": "parivahan",
  "documents": [
    {
      "document_type": "PUC Certificate",
      "document_number": "certificate-reference",
      "issue_date": "2026-01-01",
      "expiry_date": "2026-12-31",
      "verified": true
    }
  ]
}
```

Supported document types must match the FleetFlow register. Only a response explicitly identifying `source` as `parivahan` and setting `verified: true` is marked source-verified; other imports remain unverified. The API contract is an adapter contract for an authorised provider, **not a claim that Parivahan publishes this exact endpoint or JSON schema**. Sync is performed per truck from the Roadside Compliance page, and invalid/unsupported records are not imported.

## Scheduled expiry reminders

The in-app notification list persists alerts for 30, 15, 7 and 1 day before expiry, on the expiry date, and once after the document becomes overdue. Notifications are deduplicated per document, expiry date and alert threshold.

Run the reminder command daily using cron or a systemd timer. Example cron entry (adjust the application path and virtual environment):

```cron
15 8 * * * cd /home/ubuntu/TMS/expense-tracker && /home/ubuntu/TMS/expense-tracker/venv/bin/python scripts/run_compliance_reminders.py >> /home/ubuntu/TMS/expense-tracker/instance/compliance-reminders.log 2>&1
```

If the virtual environment is not named `venv`, use the Python interpreter from your actual deployment. In Docker, schedule the command as a separate one-off container/service sharing the same database volume; do not assume a cron job on the host can see the container's SQLite database unless it uses the same mounted database. This implementation creates in-app alerts; it does not send SMS and does not guarantee email delivery.

## Important limitations

- The pre-trip table is an **advisory checklist**, not a legal determination, dispatch block, or substitute for checking the exact permit/route/state requirements.
- Permit, tax, fitness, and other rules can depend on vehicle class, age, emission standard, jurisdiction, and operation. Use the dates and requirements applicable to the actual vehicle.
- Do not mark a document government-verified merely because its details were entered in FleetFlow.
- Keep document numbers and notes to the minimum needed. Do not store passwords, OTPs, or unnecessary personal data.
- This implementation adds in-app page status indicators. It does not add SMS or scheduled email delivery.
