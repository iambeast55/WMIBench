# WMIBench

A graphical interface implementation of evil-winrm/wmiexec. Allows for executing custom commands, powershell scripts, and WMI based queries. This project was mainly an experiment to see how well AI can implmenet a fully featured tool that can interact with underlying windows operations. 

## Install and run

Linux:

```bash
./install.sh
./run.sh
# After install, wmibench is also available from PATH.
```

Windows PowerShell:

```powershell
.\install.ps1
.\run.ps1
```

Windows Command Prompt:

```text
install.cmd
run.cmd
```

See [Windows setup](docs/WINDOWS_SETUP.md) and [Linux WMI testing](packaging/linux/TESTING-WMI.md).
For Linux proxychains use, see [Proxychains](docs/PROXYCHAINS.md).

## Workspace

- **Terminal:** 
- **History:** 
- **Services / Events:** 
- **Tools:** 
- **Explorer:** 
- **Multi-host:** 

Examples:

```text
sysinfo
dir C:\Windows
services
events --log Application --count 25
svc Spooler
events --help
```

## Plugins

Plugins are python based scripts that can be added. These scripts can interact with the local system and remote host, allowing for further extensibility. 
Add it through **Commands > Python commands**, or put it in the repository's `plugins` directory.

- [Write a command](docs/PYTHON_COMMANDS.md)
- [Plugin API and parameter schema](docs/PLUGIN_API.md)
- [Scheduled-task and registry plugins](docs/ADMIN_PLUGINS.md)
- [Built-in WMI command development](docs/ADDING_WMI_COMMANDS.md)

Authentication and credential storage are documented in
[Authentication](docs/AUTHENTICATION.md) and [Credentials](docs/CREDENTIALS.md).
