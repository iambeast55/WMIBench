# WMIBench Naming And Compatibility

RemoteOps is now WMIBench. The title, toolbar branding, terminal prompts, application
name, Linux desktop entry and new source/plugin bundles use the new name.

## Launch

The standalone Windows build launches directly as `WMIBench.exe`; Linux builds use
`wmibench`. No Python installation is needed. See [Standalone Deployment](STANDALONE.md).

For existing source/Python installations:

```text
wmibench
python -m wmibench
```

On Windows, use `.venv\Scripts\wmibench.exe` or
`.venv\Scripts\python.exe -m wmibench` from your installation directory.
The old `remoteops` launcher and `python -m remoteops` remain supported aliases.
Linux's installer supplies both launchers and updates the existing desktop entry
to WMIBench without adding a duplicate menu entry.

## Preserved Identifiers

No data migration, password re-entry or plugin edits are required for the rename:

- The pip distribution remains `remoteops` so installation upgrades the existing
  package rather than installing a second distribution owning the same files.
- Plugin imports continue to use `remoteops.commands.plugin_api`. The `wmibench`
  module is an application launcher, not a replacement plugin API namespace.
- Saved data remains under `%USERPROFILE%\AppData\Local\RemoteOps` on Windows
  and `~/.local/share/RemoteOps` on Linux.
- The SQLite filename, encryption key filename, legacy keyring service name and
  settings file are unchanged. Installed plugin folders are retained; legacy trust
  records are ignored because plugin discovery is now automatic.
- Linux's default code installation remains `~/.local/share/remoteops` and its
  desktop entry ID remains `remoteops.desktop` for upgrades and existing shortcuts.
- `WMIBENCH_INSTALL_DIR` and `WMIBENCH_BIN_DIR` take precedence when set; legacy
  `REMOTEOPS_INSTALL_DIR` and `REMOTEOPS_BIN_DIR` remain supported fallbacks.
- Internal protocol helper names, temporary-file prefixes, diagnostic modules and
  historical archives keep their existing identifiers.

The current application bundles are `wmibench-preview.zip` and
`wmibench-preview.tar.gz`. The current optional plugin pack is
`wmibench-task-registry-plugins.zip`, including the scheduled-task XML creation fix.
