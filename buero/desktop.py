"""Büro as a Mac app: double-click, a menu bar icon appears and the browser opens.

Used by the bundled app (packaging/macos). The bundle carries Python, Pango and the other PDF
libraries, so nothing has to be installed. Quitting from the menu bar icon stops the app.
"""
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "Büro"
PORTS = range(8000, 8010)


def bundle_dir() -> Path | None:
    """Folder with the bundled libraries when running as the packaged app, else None."""
    return Path(sys._MEIPASS) if getattr(sys, "frozen", False) else None  # noqa: SLF001


def prepare_environment() -> None:
    """Point the macOS loader and fontconfig at the bundled libraries. The loader reads
    DYLD_FALLBACK_LIBRARY_PATH only at process start, so the app restarts itself once."""
    base = bundle_dir()
    if base is None:
        return
    if "FONTCONFIG_FILE" not in os.environ:
        (Path.home() / "Library" / "Caches" / "Buero" / "fontconfig").mkdir(parents=True, exist_ok=True)
        os.environ["FONTCONFIG_FILE"] = str(base / "fonts" / "fonts.conf")
    if sys.platform == "darwin" and not os.environ.get("BUERO_LIBS_SET"):
        env = dict(os.environ, BUERO_LIBS_SET="1", DYLD_FALLBACK_LIBRARY_PATH=f"{base}:/usr/lib")
        os.execve(sys.executable, [sys.executable, *sys.argv[1:]], env)


def running_buero(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/static/app.css", timeout=0.5) as r:
            return r.status == 200 and b"--accent" in r.read(4000)
    except OSError:
        return False


def free_port() -> int | None:
    for port in PORTS:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return None


def selftest() -> int:
    """Used by the build: run the real app on a temporary data folder and make an invoice PDF
    with the bundled libraries (templates, Pango, fonts)."""
    import tempfile
    import warnings

    warnings.simplefilter("always")
    caught = warnings.catch_warnings(record=True)
    seen = caught.__enter__()
    from . import pdf
    if err := pdf.engine_error():
        print(err)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BUERO_DATA_DIR"] = tmp
        from fastapi.testclient import TestClient

        from . import db
        db.init(Path(tmp) / "buero.sqlite3")
        from .web import app
        c = TestClient(app)
        c.post("/settings", data={"name": "Test Person", "street": "Teststr. 1", "postal_code": "10999",
                                  "city": "Berlin", "tax_number": "12/345/67890", "iban": "DE89370400440532013000",
                                  "kleinunternehmer": "on", "invoice_language": "de", "number_digits": "2",
                                  "payment_terms_days": "14"})
        c.post("/clients/save", data={"name": "Kunde GmbH", "city": "Berlin", "country": "de"})
        c.post("/invoices/new", data={"client_id": "1"})
        r = c.post("/invoices/1/save", data={"client_id": "1", "service_from": "2026-01-05",
                                             "description": ["Zirkuspädagogik"], "quantity": ["2"], "unit": ["Std."],
                                             "unit_price": ["30"], "action": "preview"})
        ok = r.headers.get("content-type") == "application/pdf" and r.content.startswith(b"%PDF")
        dash = c.get("/")
        if any("No fonts configured" in str(w.message) for w in seen):
            print("selftest: fontconfig found no fonts")
            ok = False
        print(f"selftest: invoice PDF {'ok' if ok else 'FAILED'} ({len(r.content)} bytes), dashboard {dash.status_code}")
        return 0 if ok and dash.status_code == 200 else 1


def serve(port: int) -> None:
    import uvicorn

    from . import config, db
    path = config.db_path()
    db.backup_on_start(path)
    db.init(path)
    from .web import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main() -> int:
    prepare_environment()
    if "--selftest" in sys.argv:
        return selftest()
    for port in PORTS:  # already running (opened twice): just show it
        if running_buero(port):
            webbrowser.open(f"http://127.0.0.1:{port}")
            return 0
    port = free_port()
    if port is None:
        print("No free port between 8000 and 8009.")
        return 1
    url = f"http://127.0.0.1:{port}"
    threading.Thread(target=serve, args=(port,), daemon=True).start()
    for _ in range(100):
        if running_buero(port):
            break
        time.sleep(0.1)
    webbrowser.open(url)
    try:
        menu_bar(url)
    except ImportError:  # no menu bar support (e.g. running from source): keep serving
        while True:
            time.sleep(3600)
    return 0


def menu_bar(url: str) -> None:
    import rumps

    from . import config

    class BueroApp(rumps.App):
        def __init__(self):
            icon = bundle_dir() / "menubar.png" if bundle_dir() else None
            super().__init__(APP_NAME, title=None if icon and icon.exists() else APP_NAME,
                             icon=str(icon) if icon and icon.exists() else None, template=True, quit_button=None)
            self.menu = [rumps.MenuItem("Open Büro", callback=lambda _: webbrowser.open(url)),
                         rumps.MenuItem("Show my data folder", callback=lambda _: os.system(f'open "{config.data_dir()}"')),
                         None, rumps.MenuItem("Quit Büro", callback=lambda _: rumps.quit_application())]

    BueroApp().run()


if __name__ == "__main__":
    sys.exit(main())
