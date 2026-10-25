# PyInstaller recipe for Büro.app. Built on a Mac by .github/workflows/mac-app.yml:
#   DYLD_FALLBACK_LIBRARY_PATH=$(brew --prefix)/lib uv run pyinstaller packaging/macos/buero.spec
# The weasyprint hook (pyinstaller-hooks-contrib) copies Pango, HarfBuzz, fontconfig and their
# dependencies from Homebrew into the bundle.
import os
import re
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

def _own_copies(binaries, datas):
    """Pillow brings its own libharfbuzz.0.dylib (and freetype, png...). PyInstaller matches
    libraries by file name, so it links Pango to Pillow's copy (a symlink in the top folder), a
    different build than the HarfBuzz WeasyPrint opens. Use Homebrew's files there instead."""
    brew_lib = Path(os.environ.get("HOMEBREW_PREFIX", "/opt/homebrew")) / "lib"
    replaced = set()
    for dest, src, kind in datas + binaries:
        if kind == "SYMLINK" and "/" not in dest and "/" in src and (brew_lib / dest).exists():
            print(f"buero.spec: {dest} linked to {src}, using Homebrew's copy")
            replaced.add(dest)
    drop = lambda toc: [e for e in toc if not (e[2] == "SYMLINK" and e[0] in replaced)]
    binaries = drop(binaries) + [(n, os.path.realpath(brew_lib / n), "BINARY") for n in sorted(replaced)]
    return binaries, drop(datas)


# Homebrew ships each library under two names (libharfbuzz.dylib -> libharfbuzz.0.dylib) and the
# hook copies both. Two copies load as two separate libraries, so make the plain name a symlink.
def _dedupe(binaries):
    dylibs = {dest for dest, _src, kind in binaries if kind == "BINARY" and dest.endswith(".dylib")}
    links = {}
    for dest in dylibs:
        folder, name = os.path.split(dest)
        versioned = re.compile(re.escape(name[:-len(".dylib")]) + r"(\.\d+)+\.dylib$")
        candidates = sorted(d for d in dylibs if os.path.dirname(d) == folder and versioned.match(os.path.basename(d)))
        if candidates:
            links[dest] = os.path.basename(candidates[0])
    for dest, src, kind in binaries:
        if dest in links or "harfbuzz" in dest:
            print(f"buero.spec: {dest} ({src}) -> {links.get(dest, 'kept')}")
    return [(d, links[d], "SYMLINK") if d in links else (d, src, kind) for d, src, kind in binaries]


a.binaries, a.datas = _own_copies(a.binaries, a.datas)
a.binaries = _dedupe(a.binaries)
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
