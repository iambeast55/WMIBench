# Standalone Deployment

One executable per operating system, with Python, Qt, native libraries and application
dependencies bundled. End users do not need Python, pip, a virtual environment or
administrator privileges to install the app. Remote access permissions still apply.

## Portable bundles

Use `python packaging/standalone/build.py --bundle` on the target operating system.
This creates `dist/WMIBench-windows-x86_64/` or `dist/WMIBench-linux-x86_64/`.
Each bundle contains the executable, launcher, installer, `plugins/`, and a
portable marker. Run `run.cmd`/`run.ps1` on Windows or `./run.sh` on Linux.
The bundle keeps `commands.json`, `remoteops.sqlite3`, and plugins in that same
folder. The first run creates the command library with the built-in commands.

Windows installation is optional:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Linux installation is optional:

```bash
bash ./install.sh
```

The Linux executable must be built on Linux; the Windows build cannot produce it.

## Run And Update

- Windows x64: run `dist/WMIBench-windows-x86_64/run.cmd` or `run.ps1`.
- Linux x64: run `dist/WMIBench-linux-x86_64/run.sh`.
  The bundle preserves executable permissions and includes the install script.
- Close the old app and replace the executable to update. Do not remove the data folder.
- Windows data/plugins stay in `%USERPROFILE%\AppData\Local\RemoteOps`;
  Linux uses `~/.local/share/RemoteOps`. Existing hosts, encrypted credentials and
  history are reused under the same OS account. Launching the executable does not
  copy or delete an existing Python installation.
- Plugins remain ordinary editable folders under that data directory, not inside the
  executable. Drop in a folder or use Install folder; saved edits load on the next run.

The single executable extracts its bundled runtime into a temporary directory at launch.
First startup may take longer and needs free temporary disk space. Linux needs a
desktop session, a glibc-based distribution and Qt's normal system graphics/X11
libraries; it is not a universal static binary for every Linux distribution.
The CI baseline is Ubuntu 22.04 x64 (glibc 2.35). The x64 bundle targets glibc
2.35 or newer, including Ubuntu 22.04+ and Debian 12+. Alpine/musl and ARM are
not covered by the supplied builds. Use the source installer for older Debian.
Windows artifacts are unsigned previews; organizational application-control policies
may require signing/approval. Do not disable endpoint protection to run a build.

## Self-build

Build locally on the target OS. PyInstaller does not cross-compile.

Windows PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip install -r packaging/standalone/requirements.txt
.\.venv\Scripts\python.exe packaging/standalone/build.py --bundle
```

Linux:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install . -r packaging/standalone/requirements.txt
python packaging/standalone/build.py --bundle
```

Use Python 3.13 for the supplied build/test baseline. The build writes to a platform
folder under `dist` and generates a SHA-256 sidecar. It includes font/icon
resources, the session-management PowerShell asset, pywin32 on Windows, and dynamic
keyring backends. PyInstaller is pinned; application dependency ranges remain those
in `pyproject.toml`, so rebuilding later may select newer compatible dependencies.

The repository does not build executables for ordinary source changes. If you use the
optional manual workflow, it runs the same build and tests on Windows and Linux, then
uploads artifacts. Linux must be built on Linux, not cross-compiled on Windows:
[PyInstaller operating model](https://pyinstaller.org/en/latest/operating-mode.html).

Plugins may import the app's public API, bundled dependencies and included standard
library modules. Installing a new third-party Python dependency into a separate system
Python does not add it to this executable; new dependencies require a new bundled build.

## Offline Verification

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\WMIBench.exe --self-test
.\WMIBench.exe --plugin-cli test tools/auth-package-check --trust
.\WMIBench.exe --plugin-cli test C:\path\to\scheduled-task --trust
```

```bash
QT_QPA_PLATFORM=offscreen ./wmibench --self-test
./wmibench --plugin-cli test /path/to/registry --trust
```

The self-test uses temporary data, opens an offscreen GUI, checks resources, database,
crypto and transport imports, and runs a plugin against fake responses. It never
contacts a remote target or changes existing application data. The GUI's Run offline
tests action relaunches this same executable, not an external Python interpreter.

Build and verification status is reported with the delivered artifact. This Windows
workspace has no Linux WSL distribution; the Linux recipe/CI is provided, but a Linux
binary must be built and verified on a Linux runner before distribution. Offline
checks do not establish live WMI/WinRM compatibility with every target.
