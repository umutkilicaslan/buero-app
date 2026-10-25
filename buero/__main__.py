import argparse
import os
import sys
import threading
import webbrowser

import uvicorn

from . import config, db


HOMEBREW_LIB = "/opt/homebrew/lib"


def _find_homebrew_libs():
    """On Apple Silicon, Homebrew puts Pango in /opt/homebrew/lib, where Python does not look.
    The macOS loader reads DYLD_FALLBACK_LIBRARY_PATH only at process start, so restart once with it set."""
    if sys.platform != "darwin" or not os.path.isdir(HOMEBREW_LIB) or os.environ.get("BUERO_LIBS_SET"):
        return
    paths = [p for p in os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "").split(":") if p]
    if HOMEBREW_LIB in paths:
        return
    env = dict(os.environ, BUERO_LIBS_SET="1",
               DYLD_FALLBACK_LIBRARY_PATH=":".join([HOMEBREW_LIB, *paths, "/usr/local/lib", "/usr/lib"]))
    os.execve(sys.executable, [sys.executable, "-m", "buero", *sys.argv[1:]], env)


def main():
    _find_homebrew_libs()
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

    from . import pdf
    if err := pdf.engine_error():
        print(f"\nWarning: {err}\n")

    from .web import app
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
