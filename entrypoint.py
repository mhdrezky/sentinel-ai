"""Frozen-binary entry point.

PyInstaller runs its entry script as a top-level module, so `__main__.py`'s
relative imports do not resolve. This module uses an absolute import instead
and is the script the spec file points at.
"""

import sys

from sentinel_ai.main import main

if __name__ == "__main__":
    sys.exit(main())
