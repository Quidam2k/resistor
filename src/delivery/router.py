"""Route letters to the right delivery channel per representative."""

from src.config import load_representatives
from src.delivery.email_sender import send_email
from src.delivery.fax_sender import send_fax, format_fax_number


# Default delivery channel per representative
# These can be overridden per-letter at send time
DELIVERY_DEFAULTS = {
    "James Manning Jr.": {"channel": "email", "address": "Sen.JamesManning@oregonlegislature.gov"},
    "Julie Fahey": {"channel": "email", "address": "Rep.JulieFahey@oregonlegislature.gov"},
    "Jeff Merkley": {"channel": "email", "address": "senator@merkley.senate.gov"},
    "Ron Wyden": {"channel": "fax", "fax_number": "202-228-2717"},
    "Val Hoyle": {"channel": "web_form", "url": "https://hoyle.house.gov/contact/email-val"},
    "Supreme Court": {"channel": "mail", "address": "Supreme Court of the United States, 1 First Street NE, Washington, DC 20543"},
}


def get_delivery_info(recipient_name: str) -> dict:
    """Get default delivery channel info for a representative."""
    return DELIVERY_DEFAULTS.get(recipient_name, {"channel": "unknown"})


def deliver_letter(recipient_name: str, subject: str, body: str,
                   channel_override: str = None, dry_run: bool = True) -> dict:
    """Send a letter through the appropriate channel.

    Args:
        recipient_name: Name of the representative
        subject: Letter subject/topic
        body: Full letter text
        channel_override: Force a specific channel instead of default
        dry_run: If True, preview without sending

    Returns:
        dict with delivery status and details
    """
    info = get_delivery_info(recipient_name)
    channel = channel_override or info.get("channel", "unknown")

    if channel == "email":
        address = info.get("address")
        if not address:
            return {"status": "error", "error": f"No email address for {recipient_name}"}
        return send_email(address, subject, body, dry_run=dry_run)

    elif channel == "fax":
        fax_number = info.get("fax_number")
        if not fax_number:
            return {"status": "error", "error": f"No fax number for {recipient_name}"}
        fax_digits = format_fax_number(fax_number)
        return send_fax(fax_digits, body, recipient_name, dry_run=dry_run)

    elif channel == "web_form":
        url = info.get("url", "")
        return {
            "status": "manual",
            "channel": "web_form",
            "recipient": recipient_name,
            "url": url,
            "instructions": f"Copy the letter text and paste it into the web form at:\n{url}",
            "body": body,
        }

    elif channel == "mail":
        address = info.get("address", "")
        return {
            "status": "manual",
            "channel": "mail",
            "recipient": recipient_name,
            "address": address,
            "instructions": f"Physical mail to:\n{address}\n\n(Lob API integration planned)",
            "body": body,
        }

    else:
        return {"status": "error", "error": f"Unknown channel '{channel}' for {recipient_name}"}


def show_delivery_plan(recipients: list[str]) -> str:
    """Show what channel each recipient would use."""
    lines = ["Delivery plan:", ""]
    for name in recipients:
        info = get_delivery_info(name)
        channel = info.get("channel", "unknown")
        detail = info.get("address") or info.get("fax_number") or info.get("url") or ""
        lines.append(f"  {name}: {channel} -> {detail}")
    return "\n".join(lines)
