import sys
from email.utils import parsedate_to_datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import src.delivery.email_sender as es


RECIPIENT = "office@example.net"
SUBJECT = "Please support this bill"
BODY = "I am writing to ask for your support."


@pytest.fixture
def email_setup(monkeypatch):
    env = {
        "SMTP_FROM_ADDRESS": "todd@example.org",
        "SMTP_USER": "todd@example.org",
        "SMTP_HOST": "smtp.example.org",
        "SMTP_PORT": "587",
    }
    user = {
        "name": "Todd Example",
        "address": "123 Main Street",
        "city": "Seattle",
        "state": "WA",
        "zip": "98101",
    }
    keyring = SimpleNamespace(
        get_password=Mock(return_value="keyring-password"),
        set_password=Mock(),
    )

    class FakeSMTP:
        instances = []
        login_error = None

        def __init__(self, host, port, timeout):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.calls = []
            self.login_args = None
            self.sent_message = None
            self.instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def starttls(self, *, context):
            self.calls.append("starttls")

        def login(self, username, password):
            self.calls.append("login")
            self.login_args = (username, password)
            if self.login_error is not None:
                raise self.login_error

        def send_message(self, message, to_addrs=None):
            self.calls.append("send_message")
            message.as_string()  # serialize, as the real client does
            self.sent_message = message
            self.to_addrs = to_addrs

    monkeypatch.setattr(es, "get_env", env.get)
    monkeypatch.setattr(es, "load_user", lambda: user.copy())
    monkeypatch.setattr(es.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setitem(sys.modules, "keyring", keyring)

    return SimpleNamespace(env=env, user=user, keyring=keyring, smtp=FakeSMTP)


def test_default_dry_run_skips_smtp_and_keyring(email_setup):
    result = es.send_email(RECIPIENT, SUBJECT, BODY)

    assert result["status"] == "dry_run"
    assert not email_setup.smtp.instances
    email_setup.keyring.get_password.assert_not_called()


def test_real_send_uses_keyring_credentials_and_sets_headers(email_setup):
    result = es.send_email(RECIPIENT, SUBJECT, BODY, dry_run=False)

    assert result["status"] == "sent"
    email_setup.keyring.get_password.assert_called_once_with(
        "resistor-smtp", "todd@example.org"
    )
    (smtp,) = email_setup.smtp.instances
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example.org", 587, 30)
    assert smtp.calls == ["starttls", "login", "send_message"]
    assert smtp.login_args == ("todd@example.org", "keyring-password")
    assert smtp.to_addrs == [RECIPIENT]

    message = smtp.sent_message
    assert message["To"] == RECIPIENT
    assert message["Subject"] == SUBJECT
    assert message["Date"] is not None
    assert parsedate_to_datetime(message["Date"]).tzinfo is not None
    assert message["Message-ID"].endswith("@example.org>")
    assert result["message_id"] == message["Message-ID"]
    assert message.get_content_charset() == "utf-8"


@pytest.mark.parametrize(
    "email",
    ["todd.reply@example.net", None, ""],
    ids=["present", "missing", "empty"],
)
def test_reply_to_uses_constituent_email_when_present(email_setup, email):
    if email is not None:
        email_setup.user["email"] = email

    result = es.send_email(RECIPIENT, SUBJECT, BODY, dry_run=False)

    assert result["status"] == "sent"
    (smtp,) = email_setup.smtp.instances
    assert smtp.sent_message["Reply-To"] == (email or None)
    assert result["reply_to"] == (email or None)


def test_to_self_uses_sender_address_and_preserves_intended_recipient(email_setup):
    result = es.send_email(RECIPIENT, SUBJECT, BODY, dry_run=False, to_self=True)

    assert result["status"] == "sent"
    (smtp,) = email_setup.smtp.instances
    assert smtp.sent_message["To"] == "todd@example.org"
    assert smtp.sent_message["Subject"] == f"[TEST to self] {SUBJECT}"
    assert result["to"] == "todd@example.org"
    assert result["intended_to"] == RECIPIENT


def test_missing_keyring_falls_back_to_env_password(email_setup, monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)
    email_setup.env["SMTP_PASSWORD"] = "env-password"

    result = es.send_email(RECIPIENT, SUBJECT, BODY, dry_run=False)

    assert result["status"] == "sent"
    (smtp,) = email_setup.smtp.instances
    assert smtp.login_args == ("todd@example.org", "env-password")


def test_keyring_runtime_error_falls_back_to_env_password(email_setup):
    email_setup.keyring.get_password.side_effect = RuntimeError("no usable backend")
    email_setup.env["SMTP_PASSWORD"] = "env-password"

    result = es.send_email(RECIPIENT, SUBJECT, BODY, dry_run=False)

    assert result["status"] == "sent"
    email_setup.keyring.get_password.assert_called_once_with(
        "resistor-smtp", "todd@example.org"
    )
    (smtp,) = email_setup.smtp.instances
    assert smtp.login_args == ("todd@example.org", "env-password")


@pytest.mark.parametrize("missing_credential", ["SMTP_USER", "SMTP_PASSWORD"])
def test_missing_credentials_prevent_smtp_connection(email_setup, missing_credential):
    if missing_credential == "SMTP_USER":
        del email_setup.env["SMTP_USER"]
    else:
        email_setup.keyring.get_password.return_value = None

    result = es.send_email(RECIPIENT, SUBJECT, BODY, dry_run=False)

    assert result["status"] == "error"
    assert "SMTP credentials not configured" in result["error"]
    assert not email_setup.smtp.instances


def test_smtp_authentication_error_redacts_password(email_setup):
    email_setup.keyring.get_password.return_value = "hunter2"
    email_setup.smtp.login_error = es.smtplib.SMTPAuthenticationError(
        535, b"bad pw hunter2"
    )

    result = es.send_email(RECIPIENT, SUBJECT, BODY, dry_run=False)

    assert result["status"] == "error"
    assert "hunter2" not in result["error"]
    assert result["error"] == "SMTPAuthenticationError: SMTP 535"


def test_error_text_never_passes_through_exception_message(email_setup):
    email_setup.smtp.login_error = RuntimeError("echo a2V5cmluZy1wYXNzd29yZA==")

    result = es.send_email(RECIPIENT, SUBJECT, BODY, dry_run=False)

    assert result == {"status": "error", "error": "RuntimeError"}


@pytest.mark.parametrize("field", ["subject", "to"])
def test_line_breaks_in_headers_are_rejected_before_smtp(email_setup, field):
    subject, to = SUBJECT, RECIPIENT
    if field == "subject":
        subject = "Hi\r\nBcc: x@evil.example"
    else:
        to = "office@example.net\nBcc: x@evil.example"

    result = es.send_email(to, subject, BODY, dry_run=False)

    assert result["status"] == "error"
    assert not email_setup.smtp.instances


def test_to_self_refuses_a_from_address_that_is_not_the_login(email_setup):
    email_setup.env["SMTP_FROM_ADDRESS"] = "someone-else@example.org"

    result = es.send_email(RECIPIENT, SUBJECT, BODY, dry_run=False, to_self=True)

    assert result["status"] == "error"
    assert not email_setup.smtp.instances


def test_to_self_with_display_name_sends_only_to_the_mailbox(email_setup):
    email_setup.env["SMTP_FROM_ADDRESS"] = "Todd <todd@example.org>"

    result = es.send_email(RECIPIENT, SUBJECT, BODY, dry_run=False, to_self=True)

    assert result["status"] == "sent"
    (smtp,) = email_setup.smtp.instances
    assert smtp.to_addrs == ["todd@example.org"]


def test_utf8_payload_round_trips_with_constituent_header(email_setup):
    body = "Please protect our neighborhood café."

    result = es.send_email(RECIPIENT, SUBJECT, body, dry_run=False)

    assert result["status"] == "sent"
    (smtp,) = email_setup.smtp.instances
    message = smtp.sent_message
    payload = message.get_payload(decode=True).decode(message.get_content_charset())
    assert payload == f"Todd Example\n123 Main Street\nSeattle, WA 98101\n\n{body}"


def test_dry_run_without_sender_uses_local_message_id_domain(email_setup):
    del email_setup.env["SMTP_FROM_ADDRESS"]

    result = es.send_email(RECIPIENT, SUBJECT, BODY)

    assert result["status"] == "dry_run"
    assert result["message_id"].endswith("@resistor.local>")
