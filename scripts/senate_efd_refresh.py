"""Fixed entry point: refresh Senate periodic-transaction-report filings from efdsearch.senate.gov.

No arguments. Host is hard-coded in src/rep_trades.py; it covers the senators in config/representatives.yaml only.
Accepts the eFD use agreement (5 U.S.C. app. 105(c)) at Todd's direction (msgs 67415, 67451) and stops, accepting nothing,
if the agreement text no longer matches. Run:  python -I scripts/senate_efd_refresh.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # -I drops the script dir from sys.path

from src.rep_trades import senate_main

if __name__ == "__main__":
    raise SystemExit(senate_main())
