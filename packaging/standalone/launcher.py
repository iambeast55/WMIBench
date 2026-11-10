import os
import sys
from multiprocessing import freeze_support

if sys.platform.startswith("linux"):
    os.environ.setdefault("GIO_USE_VFS", "local")

from remoteops.standalone import main


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
