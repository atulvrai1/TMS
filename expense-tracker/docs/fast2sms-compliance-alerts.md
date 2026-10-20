# Fast2SMS compliance expiry alerts

FleetFlow can send administrator SMS reminders for the latest compliance document record attached to each truck, driver, or trip. The scheduled worker checks once daily and uses reminder milestones of 30 days, 7 days, expiry day, and one day overdue. Successful provider submissions are recorded to prevent duplicate sends.

## 1. Create and configure Fast2SMS

1. Create/sign in to a Fast2SMS account: https://www.fast2sms.com/
2. Complete the provider's required DLT entity, sender/header, and content-template setup for production Indian business SMS.
3. Register this content template (or configure the exact text that your operator approves):
   
   `FleetFlow alert: {#var#} for {#var#}. Please check and renew the compliance document.`

   It has two variable placeholders. The message text must match the approved DLT template exactly. If your operator approves different wording, set `FAST2SMS_MESSAGE_TEMPLATE` to that wording using the `{document}` and `{vehicle}` placeholders, and make sure the template's number/order of variables match.
4. From the Fast2SMS developer/API area, copy the API key. Collect your approved sender ID, entity ID, and content-template ID.

DLT registration and approval can take time; don't assume a successful API response means the handset received the message.

## 2. Configure Docker privately

Edit `expense-tracker/.env.docker` on the server. Do not commit this file or paste the API key into chat:

```dotenv
FAST2SMS_API_KEY=your_private_api_key
FAST2SMS_SENDER_ID=YOURID
FAST2SMS_ENTITY_ID=your_approved_entity_id
FAST2SMS_TEMPLATE_ID=your_approved_template_id
# Optional; must match your approved DLT template exactly after substitutions:
FAST2SMS_MESSAGE_TEMPLATE=FleetFlow alert: {document} for {vehicle}. Please check and renew the compliance document.
COMPLIANCE_SMS_CHECK_INTERVAL_SECONDS=86400
```

Use the exact values provided by your Fast2SMS/DLT account. The default template string shown above is only a starting point; it is not automatically approved by the telecom operator.

## 3. Deploy

From the server:

```bash
cd ~/TMS/expense-tracker
sudo docker compose --env-file .env.docker -f compose.yaml up -d --build
sudo docker compose --env-file .env.docker -f compose.yaml ps
sudo docker compose --env-file .env.docker -f compose.yaml logs --tail=100 compliance-sms-worker
```

Open FleetFlow **Administration → Notifications → Mobile SMS alerts**, enter the administrator's Indian 10-digit mobile number, enable alerts, and save. Use **Send test SMS** to test provider submission after the DLT configuration is complete.

## Notes

- The SMS key is supplied as an environment variable, not stored in SQLite or source control.
- The recipient number and enable/disable preference are stored in the FleetFlow database.
- The worker shares the existing persistent database volume with the web app.
- SMS logs record submission/failure and provider request ID. `submitted` means Fast2SMS accepted the request; delivery receipts are not yet synchronized back into FleetFlow.
- The worker retries failed records on a later run, but does not resend a successfully submitted document/milestone.
- This implementation sends to the configured administrator number, not automatically to each driver's personal number.
