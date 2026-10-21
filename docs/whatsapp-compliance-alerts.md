# FleetFlow automatic WhatsApp compliance alerts

FleetFlow uses Meta's official WhatsApp Cloud API to send automatic reminders to one configured Indian WhatsApp number. The separate Docker worker checks documents once per day at 30 days before expiry, 7 days before expiry, on the expiry date, and one day overdue. Successful API submissions are deduplicated by document, recipient, and milestone.

Official API reference: https://developers.facebook.com/docs/whatsapp/cloud-api/overview/

## 1. Set up WhatsApp Cloud API

1. Open https://developers.facebook.com/ and create/select a Meta app.
2. Add the **WhatsApp** product and open **WhatsApp > API Setup**.
3. For initial testing, use the test business phone number and recipient number Meta allows you to add. Verify the recipient number with the one-time code.
4. Copy the **Phone Number ID** (not the visible phone number) and an access token from the API Setup page.
5. For ongoing use, configure a permanent/system-user token and your own eligible business phone number in WhatsApp Manager. Keep tokens private.
6. Make sure the recipient has opted in to receive FleetFlow compliance messages.

## 2. Create and approve the Utility template

In WhatsApp Manager, create a message template with:
- Name: `fleetflow_compliance_alert`
- Category: **Utility**
- Language: **English** (`en`)
- Body text: `FleetFlow compliance alert: {{1}} for {{2}}. {{3}}. Please renew the document before expiry.`

The template must have exactly three body text variables in this order:
1. Document type (e.g. Insurance)
2. Vehicle or driver label (e.g. BR-XX-1234)
3. Expiry status/date (e.g. Expires in 7 days on 2026-10-27)

Wait until Meta approves the template. The default configured template name and language must match your approved template exactly. Business-initiated reminders outside the 24-hour customer-service window must use an approved template.

## 3. Configure FleetFlow

Edit `~/TMS/expense-tracker/.env.docker` (create it from `.env.docker.example` if needed) and set:

```dotenv
WHATSAPP_ACCESS_TOKEN=YOUR_PRIVATE_ACCESS_TOKEN
WHATSAPP_PHONE_NUMBER_ID=YOUR_PHONE_NUMBER_ID
WHATSAPP_API_VERSION=v23.0
WHATSAPP_TEMPLATE_NAME=fleetflow_compliance_alert
WHATSAPP_TEMPLATE_LANGUAGE=en
COMPLIANCE_SMS_CHECK_INTERVAL_SECONDS=86400
```

Use the Graph API version supported by your Meta app if it differs from the example. Do not commit `.env.docker`, paste tokens into chat, or put secrets in Git.

## 4. Rebuild and start the app

Check local changes before switching branches; do not discard uncommitted work. If you are using this PR branch:

```bash
cd ~/TMS
git status
git fetch origin
git switch feat/msg07-compliance-sms
git pull --ff-only origin feat/msg07-compliance-sms
cd ~/TMS/expense-tracker
sudo docker compose --env-file .env.docker -f compose.yaml up -d --build
sudo docker compose --env-file .env.docker -f compose.yaml ps
sudo docker compose --env-file .env.docker -f compose.yaml logs --tail=100 compliance-sms-worker
```

## 5. Enable and test

1. Sign in to FleetFlow as an administrator.
2. Open **Administration → Notifications → WhatsApp compliance alerts**.
3. Enter the single Indian WhatsApp recipient number, enable automatic alerts, and save.
4. Click **Send test WhatsApp**.
5. Confirm the message arrives on the recipient's WhatsApp. Check worker logs if it fails.

The API's message ID means Meta accepted the request; it is not proof the message reached the handset. Delivery can be asynchronous. Check logs and the WhatsApp Manager message status if a test does not arrive.

## Cost and limitations

Meta's pricing and any provider/number fees can change. A one-recipient integration is still subject to the WhatsApp Business Platform's template, opt-in, account, and pricing rules. Do not use unofficial WhatsApp Web automation; it can violate WhatsApp terms and risk number restrictions. Review current official documentation and pricing before production use.
