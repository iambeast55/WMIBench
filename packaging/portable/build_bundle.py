"""Stage a self-contained WMIBench folder around a target executable."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )


def stage(executable: Path, output: Path, platform_name: str) -> None:
    executable = executable.resolve()
    output = output.resolve()
    if not executable.is_file():
        raise FileNotFoundError(f"Executable not found: {executable}")
    output.mkdir(parents=True, exist_ok=True)

    for child in output.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    target_name = "WMIBench.exe" if platform_name == "windows" else "wmibench"
    shutil.copy2(executable, output / target_name)
    digest = executable.with_suffix(executable.suffix + ".sha256")
    if digest.is_file():
        shutil.copy2(digest, output / (target_name + ".sha256"))

    (output / "wmibench.portable").write_text(
        "WMIBench portable data marker. Keep this file beside the executable.\n",
        encoding="utf-8",
    )
    plugins = output / "plugins"
    plugins.mkdir()
    (plugins / "README.md").write_text(
        "Drop plugin folders containing command.json and implementation.py here. "
        "They load automatically on the next command dispatch.\n",
        encoding="utf-8",
    )
    examples = Path(__file__).resolve().parents[2] / "examples" / "plugins"
    for plugin in examples.iterdir():
        if plugin.is_dir():
            _copy_tree(plugin, plugins / plugin.name)

    templates = Path(__file__).resolve().parent
    if platform_name == "windows":
        for name in ("run.ps1", "run.cmd", "install.ps1", "install.cmd"):
            shutil.copy2(templates / name, output / name)
    else:
        for name in ("run.sh", "install.sh"):
            destination = output / name
            shutil.copy2(templates / name, destination)
            destination.chmod(destination.stat().st_mode | 0o111)

    readme = {
        "windows": "run.ps1",
        "linux": "run.sh",
    }[platform_name]
    (output / "README.md").write_text(
        f"# WMIBench portable bundle\n\n"
        f"Run `{readme}` from this folder. The command library, database, and plugins "
        f"remain beside the executable.\n\n"
        f"Add plugins under `plugins/`; they are ordinary trusted local Python code.\n",
        encoding="utf-8",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=("windows", "linux"))
    args = parser.parse_args(argv)
    stage(args.executable, args.output, args.platform)
    print(json.dumps({"bundle": str(args.output.resolve()), "platform": args.platform}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
