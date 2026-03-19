"""Load project configuration files."""

import os
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_representatives() -> dict:
    return load_yaml(CONFIG_DIR / "representatives.yaml")


def load_user() -> dict:
    return load_yaml(CONFIG_DIR / "user.yaml")


def get_env(key: str, default: str = None) -> str:
    """Get environment variable, loading .env if present."""
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    return os.getenv(key, default)
