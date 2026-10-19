"""Send letters via physical mail using the Lob API.

API details:
- Base URL: https://api.lob.com/v1
- Auth: HTTP Basic, API key as username, empty password
- Test keys (test_*) create letters that render thumbnails but are never
  printed or mailed and cost nothing. Live keys (live_*) print and mail
  for real (~$1-2 per one-page letter) and require a payment method.
"""

import requests

from src.config import get_env, load_user


LOB_BASE = "https://api.lob.com/v1"


def _api_key(use_live: bool = False) -> str:
    key_name = "LOB_API_KEY" if use_live else "LOB_TEST_API_KEY"
    api_key = get_env(key_name)
    if not api_key:
        raise RuntimeError(f"{key_name} not set in .env")
    return api_key


def _text_to_html(text: str) -> str:
    """Convert plain text letter to simple HTML for Lob's PDF rendering."""
    paragraphs = text.strip().split("\n\n")
    html_parts = []
    for p in paragraphs:
        lines = p.strip().replace("\n", "<br>\n")
        html_parts.append(f"<p>{lines}</p>")
    body = "\n".join(html_parts)
    return (
        '<html><head><meta charset="utf-8"></head>'
        '<body style="font-family: Times New Roman, serif; '
        'font-size: 12pt; margin: 0.75in; padding-top: 2.5in;">\n'
        f'{body}\n'
        '</body></html>'
    )


def _from_address() -> dict:
    """Build the return address from the constituent's user config."""
    user = load_user()
    return {
        "name": user["name"],
        "address_line1": user["address"],
        "address_city": user["city"],
        "address_state": user["state"],
        "address_zip": str(user["zip"]),
    }


def send_letter(to_address: dict, body: str, subject: str = "",
                recipient_name: str = "", dry_run: bool = True,
                use_live: bool = False) -> dict:
    """Send a letter via physical mail through Lob.

    Args:
        to_address: dict with name, address_line1, address_city,
            address_state, address_zip (address_line2 optional)
        body: Full letter text
        subject: Used as the Lob description for tracking
        recipient_name: Name for logging (falls back to to_address name)
        dry_run: If True, preview without calling the API at all
        use_live: If True, use the live key (real print + mail, costs money);
            otherwise the test key (renders but never mails, free)

    Returns:
        dict with status and details (includes thumbnail URLs on success)
    """
    recipient = recipient_name or to_address.get("name", "")

    if dry_run:
        return {
            "status": "dry_run",
            "to": to_address,
            "recipient": recipient,
            "mode": "live" if use_live else "test",
            "body_preview": body[:200] + "...",
        }

    try:
        api_key = _api_key(use_live)
        payload = {
            "description": subject or f"Letter to {recipient}",
            "to": to_address,
            "from": _from_address(),
            "file": _text_to_html(body),
            "color": False,
            "double_sided": False,
            "address_placement": "top_first_page",
            "use_type": "operational",
        }
        response = requests.post(
            f"{LOB_BASE}/letters",
            json=payload,
            auth=(api_key, ""),
        )
        response.raise_for_status()
        data = response.json()
        return {
            "status": "sent" if use_live else "test_created",
            "mode": "live" if use_live else "test",
            "to": to_address,
            "recipient": recipient,
            "letter_id": data.get("id", ""),
            "expected_delivery_date": data.get("expected_delivery_date", ""),
            "url": data.get("url", ""),
            "thumbnails": data.get("thumbnails", []),
            "response": data,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "response_body": getattr(getattr(e, "response", None), "text", None),
        }


def check_account(use_live: bool = False) -> dict:
    """Check if the Lob API key works with a cheap read-only call."""
    try:
        api_key = _api_key(use_live)
        response = requests.get(
            f"{LOB_BASE}/addresses",
            params={"limit": 1},
            auth=(api_key, ""),
        )
        response.raise_for_status()
        return {"status": "ok", "mode": "live" if use_live else "test",
                "response": response.json()}
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "response_body": getattr(getattr(e, "response", None), "text", None),
        }
