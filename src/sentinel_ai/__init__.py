"""Sentinel-AI — local dependency and supply-chain guard."""

__version__ = "0.3.2"

__all__ = ["__version__", "main"]


def main() -> int:
    """Console-script shim. Imported lazily to keep `--version` startup cheap."""
    from .main import main as _main

    return _main()
