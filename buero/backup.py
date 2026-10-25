"""Backups: a consistent copy of the database plus all documents in one dated ZIP."""
import argparse
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from . import config

FOLDERS = ("invoices", "receipts", "reminders", "bank-imports")


def make_backup(target_dir: Path) -> Path:
    target_dir = Path(target_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    data = config.data_dir()
    out = target_dir / f"buero-backup_{datetime.now():%Y-%m-%d_%H%M%S}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "buero.sqlite3"
        src = sqlite3.connect(config.db_path())
        dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)  # safe while the app is running
        src.close()
        dst.close()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "buero.sqlite3")
            for folder in FOLDERS:
                for f in sorted((data / folder).rglob("*")) if (data / folder).exists() else []:
                    if f.is_file():
                        z.write(f, f.relative_to(data).as_posix())
    with zipfile.ZipFile(out) as z:
        bad = z.testzip()
        if bad:
            raise RuntimeError(f"Backup is damaged ({bad}).")
    return out


def main():
    p = argparse.ArgumentParser(description="Back up Büro data (database + documents) into a ZIP file.")
    p.add_argument("target", help="Folder for the backup, e.g. /Volumes/Backup/buero")
    args = p.parse_args()
    print(make_backup(Path(args.target)))


if __name__ == "__main__":
    main()
