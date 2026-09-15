# PyInstaller spec for JobPilot.
# Ported from Prospector's installer\prospector.spec -- same one-folder
# reasoning applies unchanged: it starts in about a second instead of
# unpacking a large archive to temp on every launch, and antivirus flags
# one-file bundles far more often.
#
# NOT YET BUILD-VERIFIED. JobPilot pulls in playwright and crawl4ai, neither
# of which Prospector depends on, and neither has been frozen with
# PyInstaller anywhere in this portfolio before. build_installer.ps1's
# "does every module import cleanly" check (adapted from Prospector's) will
# catch a packaging problem with either of them in about thirty seconds --
# run that first, before assuming this spec is correct as written.
#
# Built by build_installer.ps1, which then wraps the folder in an Inno Setup
# installer. Run it from the project root:
#     pyinstaller installer\jobpilot.spec --noconfirm

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent

block_cipher = None

# Flask and its dependencies resolve a lot at runtime, so their submodules are
# collected explicitly -- otherwise the frozen app dies on the first request
# with a missing-module error that never appears in testing. Unlike
# Prospector, pandas, playwright and crawl4ai are real dependencies here, not
# excluded.
hidden = []
for package in ("flask", "jinja2", "werkzeug", "click", "itsdangerous",
                "blinker", "httpx", "httpcore", "h11", "certifi", "anyio",
                "dotenv", "typer", "rich", "yaml", "bs4", "pandas", "docx",
                "playwright", "crawl4ai", "mcp", "jobspy",
                # jobpilot.web's standalone window (webui.run(native=True)) --
                # pywebview picks its platform backend (edgechromium on
                # Windows) with a dynamic import PyInstaller's static
                # analysis can't see, and pythonnet/clr_loader load the CLR
                # the same dynamic way.
                "webview", "clr_loader", "pythonnet"):
    try:
        hidden += collect_submodules(package)
    except Exception:
        hidden.append(package)

# JobPilot's own modules are collected, never listed. Stages are imported
# lazily inside functions so PyInstaller's static analysis cannot see them, and
# a hand-written list silently rots every time a module is renamed.
hidden += ["jobpilot"] + collect_submodules("jobpilot")
hidden += ["encodings.idna"]

a = Analysis(
    [str(ROOT / "installer" / "jobpilot_launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=sorted(set(hidden)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trimming the unrelated scientific/GUI stack keeps the installer smaller
    # -- pandas itself is kept, it's a real dependency here.
    excludes=["tkinter", "matplotlib", "numpy.testing", "scipy", "PIL",
              "PySide6", "PyQt5", "notebook", "IPython", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JobPilot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX compression is a common antivirus trigger
    console=False,           # windowed: the UI is the browser, not a terminal
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "installer" / "jobpilot.ico")
        if (ROOT / "installer" / "jobpilot.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="JobPilot",
)
