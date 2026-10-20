# FleetFlow compliance SMS alerts with Msg07

FleetFlow's existing compliance expiry worker can submit SMS through Msg07. The free Msg07 plan is advertised as 100 messages per month with one connected Android device/SIM; confirm current account limits and delivery behavior in your Msg07 dashboard.

## 1. Get Msg07 API details

1. Create/sign in to your Msg07 account and activate the Free Forever plan.
2. Connect the Android phone/SIM in Msg07 as required by its setup guide.
3. In the Msg07 API documentation/dashboard, obtain the exact **send-message endpoint**, API key, authentication header/scheme, and JSON request schema for your workspace.
4. Keep the API key private. Do not commit it to GitHub or paste it into a public issue.

The public Msg07 marketing/docs pages describe REST API support but do not expose the complete endpoint/request schema in the public page text. FleetFlow therefore makes the endpoint, auth header, and JSON body configurable rather than guessing an undocumented endpoint.

## 2. Configure Docker

In `~/TMS/expense-tracker/.env.docker`, add the settings below using the values and exact request schema from your Msg07 workspace:

```dotenv
MSG07_API_URL=https://YOUR_MSG07_SEND_ENDPOINT
MSG07_API_KEY=YOUR_PRIVATE_API_KEY
MSG07_AUTH_HEADER=Authorization
MSG07_AUTH_SCHEME=Bearer
MSG07_PAYLOAD_TEMPLATE={"to":"{{mobile}}","message":"{{message}}"}
MSG07_MESSAGE_TEMPLATE=FleetFlow alert: {document} for {vehicle}. Please check and renew the compliance document.
COMPLIANCE_SMS_CHECK_INTERVAL_SECONDS=86400
```

- `MSG07_API_URL` must be the exact HTTPS endpoint from the account docs.
- The default auth header is `Authorization: Bearer <key>`. If your account uses another header, set `MSG07_AUTH_HEADER` and `MSG07_AUTH_SCHEME` to match it. For example, `X-API-Key` with a blank scheme means the key is sent directly as that header's value.
- `MSG07_PAYLOAD_TEMPLATE` must be valid JSON matching Msg07's API. Use `{{mobile}}` and `{{message}}` as the recipient and message placeholders. Rename fields or nest values as required by the documented schema.
- Keep `MSG07_MESSAGE_TEMPLATE` aligned with the supported message length and variables.
- The connected Android phone and SIM must remain available, online, and permitted to send SMS. Mobile carrier charges/limits may still apply.

## 3. Start FleetFlow

```bash
cd ~/TMS
git fetch origin
git switch feat/msg07-compliance-sms
git pull --ff-only origin feat/msg07-compliance-sms
cd ~/TMS/expense-tracker
sudo docker compose --env-file .env.docker -f compose.yaml up -d --build
sudo docker compose --env-file .env.docker -f compose.yaml ps
sudo docker compose --env-file .env.docker -f compose.yaml logs --tail=100 compliance-sms-worker
```

If your local branch has uncommitted work or the pull fails, stop and inspect `git status`; do not reset or discard local changes.

## 4. Enable and test

1. Sign in as an administrator.
2. Open **Administration → Notifications → Mobile SMS alerts**.
3. Enter the administrator's Indian mobile number, enable automatic alerts, and save.
4. Click **Send test SMS**.
5. Check the worker logs and the Msg07 dashboard for the API request and delivery status.

A successful HTTP/API submission is not proof that the handset received the SMS. Confirm the message in the phone's SMS inbox and in Msg07's delivery logs.

## Reminder behavior

FleetFlow retains the existing daily worker and sends reminders for the latest record for each asset/document pairing at 30 days, 7 days, expiry day, and one day overdue. The database log deduplicates successful submissions per document, recipient, and milestone.

The provider adapter treats a 2xx response as accepted unless the response explicitly indicates failure. Delivery-status webhooks are not currently synchronized into FleetFlow's SMS log.
