"""Where the app keeps its data. Everything personal lives outside the code folder."""
import os
from pathlib import Path

LEGACY_DATA_DIR = Path.home() / "Freelance" / "buero-daten"  # first installs used this folder
DEFAULT_DATA_DIR = Path.home() / "Documents" / "Büro"


def default_data_dir() -> Path:
    return LEGACY_DATA_DIR if LEGACY_DATA_DIR.exists() else DEFAULT_DATA_DIR


def data_dir() -> Path:
    path = Path(os.environ.get("BUERO_DATA_DIR") or default_data_dir()).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "buero.sqlite3"


def invoices_dir(year: int) -> Path:
    path = data_dir() / "invoices" / str(year)
    path.mkdir(parents=True, exist_ok=True)
    return path


def backups_dir() -> Path:
    path = data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def receipts_dir(year: int) -> Path:
    path = data_dir() / "receipts" / str(year)
    path.mkdir(parents=True, exist_ok=True)
    return path


def reminders_dir(year: int) -> Path:
    path = data_dir() / "reminders" / str(year)
    path.mkdir(parents=True, exist_ok=True)
    return path


def imports_dir() -> Path:
    path = data_dir() / "bank-imports"
    path.mkdir(parents=True, exist_ok=True)
    return path
