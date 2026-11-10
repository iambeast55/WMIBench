# WMIBench Naming

WMIBench is the product name used by the application, distribution, launchers,
desktop entry, portable bundles, data directories, and documentation.

## Launch

```text
wmibench
python -m wmibench
```

Standalone bundles launch with `WMIBench.exe` on Windows and `wmibench` on Linux.

## Existing data

New data is stored under:

- Windows: `%LOCALAPPDATA%\WMIBench`
- Linux: `~/.local/share/WMIBench`

Existing data under the previous application directory remains readable. Existing
credential databases and keys are retained; new files and keychain entries use the
WMIBench names.

The internal Python module namespace is retained for plugin compatibility. It is
not the product name and should not be used in user-facing text or launch commands.
