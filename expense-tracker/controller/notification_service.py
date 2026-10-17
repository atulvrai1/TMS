import logging

class NotificationService:
    """
    Handles system notifications.
    Currently implemented as a mock service that logs emails to a file and console.
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
