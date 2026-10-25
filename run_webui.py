"""Supervised entry point for the approval UI (#9454).

Pantheon's service supervisor starts this as `python.exe run_webui.py`. A
distinct script name keeps its process pattern from matching Pantheon's own
`python -m src.webui`.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from src.webui import main  # noqa: E402

main()
