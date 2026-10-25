# PyInstaller recipe for Büro.app. Built on a Mac by .github/workflows/mac-app.yml:
#   DYLD_FALLBACK_LIBRARY_PATH=$(brew --prefix)/lib uv run pyinstaller packaging/macos/buero.spec
# The weasyprint hook (pyinstaller-hooks-contrib) copies Pango, HarfBuzz, fontconfig and their
# dependencies from Homebrew into the bundle.
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent.parent
HERE = Path(SPECPATH)
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]

datas = [
    (str(ROOT / "buero" / "templates"), "buero/templates"),
    (str(ROOT / "buero" / "static"), "buero/static"),
    (str(ROOT / "docs"), "docs"),
    (str(HERE / "fonts.conf"), "fonts"),
]
if (HERE / "build" / "menubar.png").exists():
    datas.append((str(HERE / "build" / "menubar.png"), "."))
    datas.append((str(HERE / "build" / "menubar@2x.png"), "."))
for pkg in ("facturx", "pyphen", "weasyprint", "iso4217", "pycountry", "fastapi", "starlette"):
    datas += collect_data_files(pkg)

a = Analysis(
    [str(HERE / "launcher.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=collect_submodules("uvicorn") + collect_submodules("buero") + ["rumps"],
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Buero", console=False, argv_emulation=False,
          codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, name="Buero")
app = BUNDLE(
    coll,
    name="Buero.app",  # ASCII inside the build (codesign); the DMG names it Büro.app
    icon=str(HERE / "build" / "Buero.icns") if (HERE / "build" / "Buero.icns").exists() else None,
    bundle_identifier="app.buero.desktop",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "Büro",
        "CFBundleName": "Büro",
        "CFBundleShortVersionString": VERSION,
        "LSUIElement": True,  # lives in the menu bar, no Dock icon
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "Runs only on this Mac. Your data stays in Documents/Büro.",
    },
)
