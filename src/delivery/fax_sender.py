"""Send letters via fax using the Notifyre API.

API details reverse-engineered from GoLogic-Group/notifyre-nodejs-sdk:
- Base URL: https://api.notifyre.com/{version}
- Auth header: x-api-token
- Flow: upload document -> poll conversion -> submit fax
"""

import base64
import time

import requests

from src.config import get_env


NOTIFYRE_VERSION = "20220711"
NOTIFYRE_BASE = f"https://api.notifyre.com/{NOTIFYRE_VERSION}"


def _headers() -> dict:
    api_key = get_env("NOTIFYRE_API_KEY")
    if not api_key:
        raise RuntimeError("NOTIFYRE_API_KEY not set in .env")
    return {
        "x-api-token": api_key,
        "user-agent": NOTIFYRE_VERSION,
        "Content-Type": "application/json",
    }


def _text_to_html(text: str) -> str:
    """Convert plain text letter to simple HTML for better fax rendering."""
    paragraphs = text.strip().split("\n\n")
    html_parts = []
    for p in paragraphs:
        lines = p.strip().replace("\n", "<br>\n")
        html_parts.append(f"<p>{lines}</p>")
    body = "\n".join(html_parts)
    return (
        '<html><body style="font-family: Times New Roman, serif; '
        'font-size: 12pt; margin: 1in;">\n'
        f'{body}\n'
        '</body></html>'
    )


def _upload_document(text_content: str) -> str:
    """Upload a document for fax conversion. Returns the fileName for polling."""
    # Convert to HTML for reliable conversion (plain text times out)
    html = _text_to_html(text_content)
    b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")

    response = requests.post(
        f"{NOTIFYRE_BASE}/fax/send/conversion",
        json={"base64Str": b64, "contentType": "text/html"},
        headers=_headers(),
    )
    response.raise_for_status()
    data = response.json()

    # Response should have payload.fileName
    payload = data.get("payload", data)
    return payload.get("fileName", payload.get("fileID", ""))


def _poll_conversion(file_name: str, max_attempts: int = 15) -> str:
    """Poll until document conversion is complete. Returns the document ID."""
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            time.sleep(attempt * 3)  # Progressive backoff

        response = requests.get(
            f"{NOTIFYRE_BASE}/fax/send/conversion/{file_name}",
            headers=_headers(),
        )
        response.raise_for_status()
        data = response.json()
        payload = data.get("payload", data)

        status = payload.get("status", "")
        if status == "successful":
            return payload.get("id", file_name)
        elif status == "failed":
            raise RuntimeError(f"Document conversion failed: {data}")

        print(f"  Conversion status: {status} (attempt {attempt}/{max_attempts})")

    raise RuntimeError(f"Document conversion timed out after {max_attempts} attempts")


def _submit_fax(document_id: str, fax_number: str, recipient_name: str,
                subject: str = "") -> dict:
    """Submit the converted document as a fax."""
    payload = {
        "templateName": "",
        "faxes": {
            "clientReference": "",
            "files": [document_id],
            "header": "",
            "isHighQuality": False,
            "recipients": [{
                "type": "fax_number",
                "value": fax_number,
            }],
            "scheduledDate": None,
            "sendFrom": "",
            "senderID": "",
            "subject": subject,
            "campaignName": "",
        },
    }

    response = requests.post(
        f"{NOTIFYRE_BASE}/fax/send",
        json=payload,
        headers=_headers(),
    )
    response.raise_for_status()
    return response.json()


def send_fax(fax_number: str, body: str, recipient_name: str,
             subject: str = "", dry_run: bool = True) -> dict:
    """Send a letter via fax through Notifyre.

    Args:
        fax_number: Recipient fax number with country code, digits only (e.g. "12022282717")
        body: Full letter text
        recipient_name: Name for logging
        subject: Optional fax subject line
        dry_run: If True, preview without sending

    Returns:
        dict with status and details
    """
    if dry_run:
        return {
            "status": "dry_run",
            "to": fax_number,
            "recipient": recipient_name,
            "body_preview": body[:200] + "...",
        }

    try:
        api_key = get_env("NOTIFYRE_API_KEY")
        if not api_key:
            return {"status": "error", "error": "NOTIFYRE_API_KEY not set in .env"}

        # Step 1: Upload document
        print(f"  Uploading document for {recipient_name}...")
        file_name = _upload_document(body)

        # Step 2: Poll for conversion
        print(f"  Waiting for conversion...")
        doc_id = _poll_conversion(file_name)

        # Step 3: Submit fax
        print(f"  Sending fax to {fax_number}...")
        result = _submit_fax(doc_id, fax_number, recipient_name, subject)

        payload = result.get("payload", result)
        return {
            "status": "sent",
            "to": fax_number,
            "recipient": recipient_name,
            "fax_id": payload.get("faxID", payload.get("friendlyID", "")),
            "response": result,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "response_body": getattr(getattr(e, "response", None), "text", None),
        }


def check_account() -> dict:
    """Check if the Notifyre API key works by listing fax numbers."""
    try:
        response = requests.get(
            f"{NOTIFYRE_BASE}/fax/numbers",
            headers=_headers(),
        )
        response.raise_for_status()
        return {"status": "ok", "response": response.json()}
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "response_body": getattr(getattr(e, "response", None), "text", None),
        }


def check_prices() -> dict:
    """Check fax prices."""
    try:
        response = requests.get(
            f"{NOTIFYRE_BASE}/fax/send/prices",
            headers=_headers(),
        )
        response.raise_for_status()
        return {"status": "ok", "response": response.json()}
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "response_body": getattr(getattr(e, "response", None), "text", None),
        }


def format_fax_number(raw: str) -> str:
    """Normalize a fax number to digits-only with country code.

    '(202) 228-2717' -> '12022282717'
    '202-228-2717' -> '12022282717'
    """
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        digits = "1" + digits  # Add US country code
    return "+" + digits  # Notifyre requires + prefix
