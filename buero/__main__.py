import argparse
import threading
import webbrowser

import uvicorn

from . import config, db


def main():
    parser = argparse.ArgumentParser(description="Büro: local bookkeeping and invoicing")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    path = config.db_path()
    backup = db.backup_on_start(path)
    db.init(path)
    print(f"Data folder: {config.data_dir()}")
    if backup:
        print(f"Backup:      {backup}")

    from .web import app
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
