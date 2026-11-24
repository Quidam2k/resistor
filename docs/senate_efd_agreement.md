# Senate eFD use agreement

Source: https://efdsearch.senate.gov/search/home/ (verbatim, fetched at acceptance)
First accepted: 2026-11-24 by script (scripts/senate_efd_refresh.py), at the owner's direction (msgs 67415, 67451).

> Title 1 of the Ethics in Government Act of 1978, as amended, 5 U.S.C. app. § 105(c), states that: It shall be unlawful for any person to obtain or use a report: for any unlawful purpose; for any commercial purpose, other than by news and communications media for dissemination to the general public; for determining or establishing the credit rating of any individual; or for use, directly or indirectly, in the solicitation of money for any political, charitable, or other purpose. The Attorney General may bring a civil action against any person who obtains or uses a report for any purpose prohibited in paragraph (1) of this subsection. The court in which such action is brought may assess against such person a penalty in any amount not to exceed $10,000. Such remedy shall be in addition to any other remedy available under statutory or common law.

## Scope of use here
Constituent letters and `rep_record show` only. Rows carry use_flag=constituent_letters_only and are read only through `rep_trades.read_rows(..., purpose='constituent_letter')`, which refuses any other purpose. Never for fundraising or donation asks, credit rating, or anything for the owner's shop or Third Wish.
Stop rule: if the agreement text stops matching 105(c), or the scripted acceptance is blocked, stop and report; no workaround.
