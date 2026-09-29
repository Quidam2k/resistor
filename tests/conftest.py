"""Point every test at a scratch copy of resist.db with live creds blanked.

This runs before any src import, because src.config reads RESIST_DATA_DIR at
import time and get_env's load_dotenv never overrides a variable that is
already present (even when empty).
"""

import os
import sqlite3
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="resist-test-"))

os.environ["RESIST_DATA_DIR"] = str(SCRATCH)
os.environ["WEBUI_DRY_RUN"] = "1"
os.environ["PANTHEON_DB"] = str(SCRATCH / "no-pantheon.db")
for key in ("LOB_API_KEY", "NOTIFYRE_API_KEY", "SMTP_USER", "SMTP_PASSWORD"):
    os.environ[key] = ""

_live = sqlite3.connect(f"file:{(PROJECT_ROOT / 'data' / 'resist.db').as_posix()}?mode=ro",
                        uri=True)
_copy = sqlite3.connect(SCRATCH / "resist.db")
_live.backup(_copy)
_live.close()
_copy.close()
