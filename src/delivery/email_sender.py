"""Send letters via email (for state reps and Merkley)."""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from src.config import get_env, load_user


def send_email(to_address: str, subject: str, body: str, dry_run: bool = True) -> dict:
    """Send a letter via email.

    Args:
        to_address: Recipient email address
        subject: Email subject line
        body: Full letter text
        dry_run: If True, print what would be sent without actually sending

    Returns:
        dict with status and details
    """
    user = load_user()
    from_address = get_env("SMTP_FROM_ADDRESS")
    smtp_host = get_env("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(get_env("SMTP_PORT", "587"))
    smtp_user = get_env("SMTP_USER")
    smtp_password = get_env("SMTP_PASSWORD")

    msg = MIMEMultipart()
    msg["From"] = from_address
    msg["To"] = to_address
    msg["Subject"] = subject

    # Add constituent info header to body
    full_body = (
        f"{user['name']}\n"
        f"{user['address']}\n"
        f"{user['city']}, {user['state']} {user['zip']}\n\n"
        f"{body}"
    )
    msg.attach(MIMEText(full_body, "plain"))

    if dry_run:
        return {
            "status": "dry_run",
            "to": to_address,
            "subject": subject,
            "body_preview": full_body[:200] + "...",
        }

    if not all([from_address, smtp_user, smtp_password]):
        return {
            "status": "error",
            "error": "SMTP credentials not configured. Set SMTP_FROM_ADDRESS, SMTP_USER, "
                     "SMTP_PASSWORD in .env",
        }

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        return {"status": "sent", "to": to_address, "subject": subject}
    except Exception as e:
        return {"status": "error", "error": str(e)}
