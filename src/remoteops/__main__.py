from __future__ import annotations

import os
import sys

if sys.platform.startswith("linux"):
    # Some Debian-family installs expose a mismatched GVFS module through GLib.
    os.environ.setdefault("GIO_USE_VFS", "local")

from remoteops.ui.app import run


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())

