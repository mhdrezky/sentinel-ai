# PyInstaller build spec for the standalone Sentinel-AI binary.
#
# Build:   uv run pyinstaller sentinel.spec --clean --noconfirm
# Output:  dist/sentinel-ai(.exe)
#
# Single-file mode is deliberate: the binary is copied onto developer machines
# and referenced from a Husky hook, so one artefact with no sidecar directory
# is far easier to distribute and to pin a checksum against.

from PyInstaller.utils.hooks import collect_submodules

# Pydantic builds its validators dynamically, so its modules are not all
# discoverable by static analysis.
hidden_imports = collect_submodules("pydantic") + [
    "sentinel_ai.ai.client",
    "sentinel_ai.ai.prompts",
    "sentinel_ai.data.popular_packages",
]

# Trimmed to keep startup fast — this process runs on every commit.
#
# Kept deliberately conservative: `email`, `html` and `http` are pulled in
# transitively by httpx via urllib, and excluding them breaks the binary at
# import time. Only leaf packages nothing on the CLI path can reach are listed.
excludes = [
    "tkinter",
    "doctest",
    "sqlite3",
    "numpy",
    "PIL",
    "matplotlib",
    "IPython",
]

analysis = Analysis(
    # Not `__main__.py`: PyInstaller runs the entry script as a top-level
    # module, where that file's relative imports cannot resolve.
    ["entrypoint.py"],
    pathex=["src"],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=2,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="sentinel-ai",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-packed binaries trip endpoint protection on some fleets.
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
