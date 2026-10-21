# FleetFlow compliance SMS alerts with My SMS Gateway

FleetFlow's existing compliance-expiry worker can submit SMS jobs through My SMS Gateway. Its public pricing page advertises 100 SMS/OTP jobs per month on the Free plan with one Android device and one API key. It uses your Android phone and SIM; carrier SMS charges, plan limits, connectivity, and fair-use rules still apply. See https://mysmsgateway.com/.

## 1. Create and configure the gateway

1. Open https://mysmsgateway.com/ and choose **Get Started Free**.
2. Create an account and sign in at the dashboard.
3. Install the Android gateway app and pair your phone/SIM with the workspace.
4. In the workspace, copy the device ID and generate an API key. Keep the API key private.
5. Confirm the Android phone is online and its SIM can send SMS.

The public API documentation specifies `POST https://api.mysmsgateway.com/v1/sms/send`, Bearer-token authentication, and a JSON body containing `device_id`, `to`, `message`, `sim`, and `message_type`.

## 2. Configure Docker

Edit `~/TMS/expense-tracker/.env.docker` and add the actual API key and device ID:

```dotenv
MY_SMS_GATEWAY_API_URL=https://api.mysmsgateway.com/v1/sms/send
MY_SMS_GATEWAY_API_KEY=YOUR_PRIVATE_API_KEY
MY_SMS_GATEWAY_DEVICE_ID=YOUR_DEVICE_ID
MY_SMS_GATEWAY_SIM=1
MY_SMS_GATEWAY_MESSAGE_TYPE=transactional
MY_SMS_GATEWAY_MESSAGE_TEMPLATE=FleetFlow alert: {document} for {vehicle}. Please check and renew the compliance document.
COMPLIANCE_SMS_CHECK_INTERVAL_SECONDS=86400
```

Do not commit `.env.docker` or share the API key. The configured SIM must have SMS allowance and be kept online. The phone gateway must remain connected to Wi-Fi or mobile data to receive queued jobs.

## 3. Pull and start FleetFlow

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

1. Sign in to FleetFlow as an administrator.
2. Open **Administration → Notifications → Mobile SMS alerts**.
3. Enter the administrator's Indian mobile number, enable automatic alerts, and save.
4. Click **Send test SMS**.
5. Check the phone and My SMS Gateway dashboard.

A successful HTTP response means the provider accepted/queued the job, not necessarily that the handset received the SMS. Verify the actual delivery in the dashboard and phone inbox.

## Reminder behavior

The existing daily worker checks the latest record for each asset/document pairing at 30 days, 7 days, on the expiry date, and one day overdue. The database log deduplicates successful submissions per document, recipient, and milestone. In-app expiry notifications remain available if the SMS gateway is unavailable.
