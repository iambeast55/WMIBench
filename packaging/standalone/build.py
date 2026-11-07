"""Run on the target OS using the project's build environment."""
from pathlib import Path
import argparse
import hashlib
import os
import platform
import re
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", default="", help="Optional release suffix, preserving the existing executable")
    parser.add_argument("--bundle", action="store_true", help="Also stage a portable folder beside dist/standalone")
    args = parser.parse_args()
    if args.variant and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", args.variant):
        parser.error("variant must be lowercase letters, digits or hyphens (max 64)")
    if sys.platform not in ("win32", "linux"):
        raise SystemExit("Build on Windows or Linux; cross-compilation is not supported.")
    root = Path(__file__).resolve().parents[2]
    machine = platform.machine().lower()
    architecture = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    target = ("windows" if sys.platform == "win32" else "linux") + "-" + architecture
    if args.variant:
        target += "-" + args.variant
    output = root / "dist" / "standalone" / target
    environment = os.environ.copy()
    if sys.platform == "win32":
        # Do not bundle unrelated graphics/ICU DLLs from tools on the developer's PATH.
        windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        environment["PATH"] = os.pathsep.join(map(str, (
            Path(sys.executable).parent, Path(sys.base_prefix),
            windows / "System32", windows,
        )))
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", str(output), "--workpath", str(root / "build" / "standalone" / target),
        str(root / "packaging" / "standalone" / "wmibench.spec"),
    ], cwd=root, env=environment, check=True)
    executable = output / ("WMIBench.exe" if sys.platform == "win32" else "wmibench")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    executable.with_suffix(executable.suffix + ".sha256").write_text(
        f"{digest}  {executable.name}\n", encoding="ascii")
    print(f"Built {executable}")
    if args.bundle:
        from importlib.util import module_from_spec, spec_from_file_location
        stage_path = root / "packaging" / "portable" / "build_bundle.py"
        spec = spec_from_file_location("wmibench_build_bundle", stage_path)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        platform_name = "windows" if sys.platform == "win32" else "linux"
        bundle = root / "dist" / f"WMIBench-{platform_name}-{architecture}"
        module.stage(executable, bundle, platform_name)
        print(f"Staged {bundle}")


if __name__ == "__main__":
    main()
