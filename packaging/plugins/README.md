# WMIBench Task And Registry Plugin Pack

Requires the API v3 WMIBench application update (`wmibench-preview`).
This ZIP contains plugins, not an application installer.

In Command Library > Python commands, install the `scheduled-task` folder and then
the `registry` folder. Each becomes available immediately. Offline tests are optional;
no manual validation or activation step is needed with the current app.
Do not select this parent folder. Existing plugins are not overwritten automatically.

See [commands and limits](ADMIN_PLUGINS.md), [Windows setup](WINDOWS_SETUP.md),
[Python command authoring](PYTHON_COMMANDS.md), and the [API reference](PLUGIN_API.md).

No live remote changes were performed during development. Use disposable task names
and registry keys for testing. Task creation requires an explicit service account;
registry set overwrites values and key deletion removes a leaf key's values.
