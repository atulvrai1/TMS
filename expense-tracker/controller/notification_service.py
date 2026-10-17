import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

class NotificationService:
    """
    Sends general mock notifications and password-reset emails.
    """
    LOG_FILE = "email_mock.log"

    @staticmethod
    def send_email(recipient_email, subject, body):
        log_message = f"--- MOCK EMAIL ---\nTo: {recipient_email}\nSubject: {subject}\nBody: {body}\n------------------"

        # Log to console for immediate visibility during development
        print(f"\n[NotificationService] {log_message}\n")

        # Log to file for audit
        try:
            with open(NotificationService.LOG_FILE, "a") as f:
                f.write(log_message + "\n\n")
        except Exception as e:
            print(f"Error writing to email log: {e}")

    @staticmethod
    def send_password_reset_email(recipient_email, reset_url):
        smtp_host = os.environ.get("SMTP_HOST")
        if smtp_host:
            sender = os.environ.get("SMTP_FROM")
            if not sender:
                raise RuntimeError("SMTP_FROM must be configured when SMTP_HOST is set.")
            smtp_port = int(os.environ.get("SMTP_PORT", "587"))
            message = EmailMessage()
            message["Subject"] = "Reset your FleetFlow password"
            message["From"] = sender
            message["To"] = recipient_email
            message.set_content(
                "Use the following one-time link to reset your FleetFlow password. "
                "It expires in 30 minutes.\n\n"
                f"{reset_url}\n\n"
                "If you did not request this reset, you can ignore this message."
            )
            context = ssl.create_default_context()
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.starttls(context=context)
                username = os.environ.get("SMTP_USERNAME")
                password = os.environ.get("SMTP_PASSWORD")
                if username:
                    if not password:
                        raise RuntimeError(
                            "SMTP_PASSWORD must be configured when SMTP_USERNAME is set."
                        )
                    server.login(username, password)
                server.send_message(message)
            return

        mock_dir = Path(__file__).resolve().parents[1] / "instance"
        mock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(mock_dir, 0o700)
        mock_file = mock_dir / "email_mock.log"
        descriptor = os.open(
            mock_file,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600
        )
        os.chmod(mock_file, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as log_file:
            log_file.write(
                f"Password reset email for {recipient_email}; link expires in 30 minutes:\n"
                f"{reset_url}\n\n"
            )
