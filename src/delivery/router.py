"""Route letters to the right delivery channel per representative."""

from src.config import get_env, load_representatives
from src.delivery.email_sender import send_email
from src.delivery.fax_sender import send_fax, format_fax_number
from src.delivery.mail_sender import send_letter


# Default delivery channel per representative
# These can be overridden per-letter at send time
DELIVERY_DEFAULTS = {
    "James Manning Jr.": {"channel": "email", "address": "Sen.JamesManning@oregonlegislature.gov"},
    "Julie Fahey": {"channel": "email", "address": "Rep.JulieFahey@oregonlegislature.gov"},
    "Jeff Merkley": {"channel": "email", "address": "senator@merkley.senate.gov",
                     "fax_number": "202-228-3997",
                     "mail_address": {
                         "name": "Senator Jeff Merkley",
                         "address_line1": "531 Hart Senate Office Building",
                         "address_city": "Washington",
                         "address_state": "DC",
                         "address_zip": "20510",
                     }},
    "Ron Wyden": {"channel": "fax", "fax_number": "202-228-2717",
                  "mail_address": {
                      "name": "Senator Ron Wyden",
                      "address_line1": "221 Dirksen Senate Office Building",
                      "address_city": "Washington",
                      "address_state": "DC",
                      "address_zip": "20510",
                  }},
    "Val Hoyle": {"channel": "web_form", "url": "https://hoyle.house.gov/contact/email-val",
                  # District office — DC mail sits in security screening for weeks
                  "mail_address": {
                      "name": "Congresswoman Val Hoyle",
                      "address_line1": "940 Willamette St",
                      "address_city": "Eugene",
                      "address_state": "OR",
                      "address_zip": "97401",
                  }},
    # Party letters (#9247): DPO fax per dpo.org/contact; the DNC lists no fax
    # on democrats.org, so it goes by mail to the address on its FEC filings.
    "Democratic Party of Oregon": {"channel": "fax", "fax_number": "503-224-5335",
                                   "mail_address": {
                                       "name": "Democratic Party of Oregon",
                                       "address_line1": "1220 SW Morrison St Ste 910",
                                       "address_city": "Portland",
                                       "address_state": "OR",
                                       "address_zip": "97205",
                                   }},
    "Democratic National Committee": {"channel": "mail", "mail_address": {
        "name": "Democratic National Committee",
        "address_line1": "430 South Capitol St SE",
        "address_city": "Washington",
        "address_state": "DC",
        "address_zip": "20003",
    }},
    "Supreme Court": {"channel": "mail", "mail_address": {
        "name": "Supreme Court of the United States",
        "address_line1": "1 First Street NE",
        "address_city": "Washington",
        "address_state": "DC",
        "address_zip": "20543",
    }},
}


def get_delivery_info(recipient_name: str) -> dict:
    """Get default delivery channel info for a representative."""
    return DELIVERY_DEFAULTS.get(recipient_name, {"channel": "unknown"})


def deliver_letter(recipient_name: str, subject: str, body: str,
                   channel_override: str = None, dry_run: bool = True,
                   use_live: bool = False) -> dict:
    """Send a letter through the appropriate channel.

    Args:
        recipient_name: Name of the representative
        subject: Letter subject/topic
        body: Full letter text
        channel_override: Force a specific channel instead of default
        dry_run: If True, preview without sending
        use_live: For the mail channel, use the live Lob key (real print
            + postage) instead of the free test key

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
        mail_address = info.get("mail_address")
        if not mail_address:
            return {"status": "error", "error": f"No mail address for {recipient_name}"}
        if get_env("LOB_TEST_API_KEY") or get_env("LOB_API_KEY"):
            return send_letter(mail_address, body, subject=subject,
                               recipient_name=recipient_name, dry_run=dry_run,
                               use_live=use_live)
        # No Lob key configured — fall back to manual instructions
        formatted = "\n".join(
            v for v in [
                mail_address.get("name"),
                mail_address.get("address_line1"),
                mail_address.get("address_line2"),
                f"{mail_address.get('address_city')}, {mail_address.get('address_state')} "
                f"{mail_address.get('address_zip')}",
            ] if v
        )
        return {
            "status": "manual",
            "channel": "mail",
            "recipient": recipient_name,
            "address": formatted,
            "instructions": f"Physical mail to:\n{formatted}\n\n"
                            "(Set LOB_TEST_API_KEY / LOB_API_KEY in .env to automate)",
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
        detail = (info.get("address") or info.get("fax_number") or info.get("url")
                  or (info.get("mail_address") or {}).get("address_line1") or "")
        lines.append(f"  {name}: {channel} -> {detail}")
    return "\n".join(lines)
