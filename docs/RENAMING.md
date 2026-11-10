# WMIBench Naming

WMIBench is the product name used by the application, distribution, launchers,
data directories, and documentation.

## Launch

```text
wmibench
python -m wmibench
```

The repository launchers are `run.ps1`/`run.cmd` on Windows and `run.sh` on Linux.

## Existing data

When launched with the repository scripts, data is stored in the repository folder.
Direct `python -m wmibench` launches use the per-user WMIBench directory:

- Windows: `%LOCALAPPDATA%\WMIBench`
- Linux: `~/.local/share/WMIBench`

Existing data under the previous application directory remains readable. Existing
credential databases and keys are retained; new files and keychain entries use the
WMIBench names.

The internal Python module namespace is retained for plugin compatibility. It is
not the product name and should not be used in user-facing text or launch commands.
