"""Send constituent letters via SMTP and provide a self-test CLI.
Letters only send through the approval UI. This module never decides to send.
"""

import argparse
import getpass
import json
import smtplib
import ssl
import warnings
from email.mime.text import MIMEText
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
from pathlib import Path

from src.config import get_env, load_user


_KEYRING_SERVICE = "resistor-smtp"
_MISSING_CREDENTIALS = (
    "SMTP credentials not configured. Set SMTP_FROM_ADDRESS and SMTP_USER in .env "
    "and store the password with: python -m src.delivery.email_sender --store-password"
)


def _smtp_password(smtp_user: str) -> str | None:
    try:
        import keyring
    except ImportError:
        return get_env("SMTP_PASSWORD")

    try:
        password = keyring.get_password(_KEYRING_SERVICE, smtp_user)
    except Exception:  # no usable backend: fall back to .env rather than fail the send
        password = None
    if password is None:
        return get_env("SMTP_PASSWORD")
    return password


def _exception_message(error: Exception, password: str | None) -> str:
    """Fixed, credential-free error text: an SMTP reply or keyring error can echo the
    password in encoded form, so its message text is never passed through."""
    code = getattr(error, "smtp_code", None)
    message = f"{type(error).__name__}" + (f": SMTP {code}" if code else "")
    if password and password in message:  # belt and braces; the type name never holds it
        message = message.replace(password, "[REDACTED]")
    return message


def _no_line_breaks(**headers: str | None) -> None:
    for name, value in headers.items():
        if value and ("\r" in value or "\n" in value):
            raise ValueError(f"line break in {name} header")


def send_email(
    to_address: str,
    subject: str,
    body: str,
    dry_run: bool = True,
    *,
    to_self: bool = False,
) -> dict:
    """Preview or send a constituent letter, optionally addressed to the sender."""
    password = None
    try:
        from_address = get_env("SMTP_FROM_ADDRESS")
        if not dry_run:
            smtp_user = get_env("SMTP_USER")
            if not from_address or not smtp_user:
                return {"status": "error", "error": _MISSING_CREDENTIALS}
            password = _smtp_password(smtp_user)
            if not password:
                return {"status": "error", "error": _MISSING_CREDENTIALS}

        destination = parseaddr(from_address or "")[1] if to_self else to_address
        if to_self:
            subject = f"[TEST to self] {subject}"
            if not dry_run:
                recipients = getaddresses([from_address])
                if len(recipients) != 1 or not recipients[0][1]:
                    raise ValueError("SMTP_FROM_ADDRESS must specify one mailbox for a self-send")
                if recipients[0][1].lower() != smtp_user.lower():
                    raise ValueError("a self-send goes only to the SMTP_USER account itself")

        user = load_user()
        full_body = (
            f"{user['name']}\n"
            f"{user['address']}\n"
            f"{user['city']}, {user['state']} {user['zip']}\n\n"
            f"{body}"
        )
        reply_to = user.get("email")
        if not isinstance(reply_to, str) or not reply_to:
            reply_to = None
        _no_line_breaks(Subject=subject, To=destination, From=from_address, **{"Reply-To": reply_to})

        sender_mailbox = parseaddr(from_address or "")[1]
        domain = sender_mailbox.rpartition("@")[2] if "@" in sender_mailbox else ""
        message_id = make_msgid(domain=domain or "resistor.local")

        msg = MIMEText(full_body, "plain", "utf-8")
        msg["From"] = from_address or ""
        msg["To"] = destination or ""
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = message_id
        if reply_to is not None:
            msg["Reply-To"] = reply_to

        details = {
            "to": destination,
            "intended_to": to_address,
            "subject": subject,
            "message_id": message_id,
            "reply_to": reply_to,
        }
        if dry_run:
            return {
                "status": "dry_run",
                **details,
                "body_preview": full_body[:200] + "...",
            }

        host = get_env("SMTP_HOST", "smtp.gmail.com")
        port = int(get_env("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(smtp_user, password)
            server.send_message(msg, to_addrs=[destination])
        return {"status": "sent", **details}
    except Exception as error:
        return {"status": "error", "error": _exception_message(error, password)}


def _main() -> int:
    parser = argparse.ArgumentParser(description="Store SMTP credentials or test email delivery to yourself.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--store-password", action="store_true")
    action.add_argument("--to-self", action="store_true")
    parser.add_argument("--subject")
    parser.add_argument("--body-file", type=Path)
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    if args.store_password:
        if args.subject is not None or args.body_file is not None or args.send:
            parser.error("--subject, --body-file, and --send require --to-self")

        password = None
        try:
            smtp_user = get_env("SMTP_USER")
            if not smtp_user:
                parser.error("Set SMTP_USER in .env before storing the password.")

            import keyring

            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("SMTP password: ")
            keyring.set_password(_KEYRING_SERVICE, smtp_user, password)
        except Exception as error:
            parser.error(_exception_message(error, password))

        print(f"stored for {smtp_user}")
        return 0

    if args.subject is None or args.body_file is None:
        parser.error("--to-self requires --subject and --body-file")

    try:
        body = args.body_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        parser.error(_exception_message(error, None))

    result = send_email(
        get_env("SMTP_FROM_ADDRESS"),
        args.subject,
        body,
        dry_run=not args.send,
        to_self=True,
    )
    print(json.dumps(result))
    return int(result["status"] == "error")


if __name__ == "__main__":
    raise SystemExit(_main())
