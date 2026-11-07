"""Entry point and offline checks shared by the standalone builds."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


def plugin_cli_arguments(arguments):
    if getattr(sys, "frozen", False):
        return ["--plugin-cli", *arguments]
    return ["-m", "remoteops.commands.plugin_cli", *arguments]


def self_test():
    """Exercise packaged resources with temporary data and no remote connections."""
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication
    from cryptography.fernet import Fernet
    from pypsrp.powershell import PowerShell, RunspacePool
    import spnego
    from remoteops.commands.plugin_api import FakeContext, execute_plugin
    from remoteops.commands.templates import CommandRegistry
    from remoteops.core.database import Repository
    from remoteops.core.models import Host
    from remoteops.remoting.session_manager import SessionManager
    from remoteops.ui.main_window import MainWindow
    from remoteops.remoting.kerberos import KerberosContext, TicketProvider
    from remoteops.remoting.kerberos_http import MappedAdapter
    from remoteops.remoting.kerberos_rpc import KerberosRpcConnection
    from remoteops.core.authentication import DomainConnection
    assert KerberosContext and TicketProvider and MappedAdapter and KerberosRpcConnection
    assert DomainConnection("example.com", "192.0.2.1").target("192.0.2.2", "server") == ("server.example.com", "192.0.2.2")

    if sys.platform == "win32":
        import pythoncom
        import win32com.client
        import win32crypt
        import win32timezone
        encrypted = win32crypt.CryptProtectData(b"fixture", None, None, None, None, 1)
        assert win32crypt.CryptUnprotectData(encrypted, None, None, None, 1)[1] == b"fixture"
    assert Fernet(Fernet.generate_key())
    assert PowerShell and RunspacePool and spnego.client
    script = Path(__file__).parent / "remoting" / "terminal_sessions.ps1"
    assert "WTS" in script.read_text(encoding="utf-8")
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    with tempfile.TemporaryDirectory(prefix="wmibench-smoke-") as folder:
        registry = CommandRegistry(Path(folder) / "commands.json")
        plugin_folder = registry.plugins.scaffold("smoke-bios")
        prepared = registry.prepare("smoke-bios")
        context = FakeContext(query_results=[[{"Manufacturer": "Fixture", "SMBIOSBIOSVersion": "1"}]])
        rows = execute_plugin(prepared.plugin, context, prepared.values)
        assert rows == [{"Vendor": "Fixture", "Version": "1"}]
        process = QProcess()
        process.start(sys.executable, plugin_cli_arguments(["test", str(plugin_folder), "--trust"]))
        if not process.waitForStarted(10000):
            raise RuntimeError(process.errorString())
        if not process.waitForFinished(30000):
            process.kill()
            process.waitForFinished(5000)
            raise RuntimeError("Packaged plugin test process timed out")
        output = bytes(process.readAllStandardOutput()) + bytes(process.readAllStandardError())
        assert process.exitStatus() == QProcess.ExitStatus.NormalExit and process.exitCode() == 0, output
        assert b"Ran 1 test" in output and b"OK" in output, output
        repository = Repository(Path(folder) / "test.sqlite3")
        host = repository.save_host(Host("Offline fixture", "example.invalid"))
        repository.create_host_group("Lab")
        repository.save_inventory_order([("Lab", [host.id]), (None, [])])
        manager = SessionManager(repository=repository)
        window = MainWindow(repository, manager)
        window.show()
        app.processEvents()
        tab = window.host_tabs_by_id[host.id]
        assert window.windowTitle() == "WMIBench"
        assert tab.view_tabs.count() == 6
        assert tab.view_tabs.tabText(5) == "Explorer"
        assert window.host_tree.group_items["Lab"].childCount() == 1
        from remoteops.commands.completion import complete
        from remoteops.remoting.explorer import query_snippet
        assert complete("sys", 3, registry.templates)[0].text == "sysinfo"
        source = query_snippet("root/cimv2", "Win32_BIOS", [{"Name": "Manufacturer", "Type": "string"}], 5)
        compile(source, "explorer-example", "exec")
        assert not tab.run_button.icon().isNull()
        window.close()
        window.deleteLater()
        app.processEvents()
    print(json.dumps({"status": "ok", "platform": sys.platform,
                      "frozen": bool(getattr(sys, "frozen", False)),
                      "checks": ["GUI", "icons", "SQLite", "crypto", "transports", "plugins",
                                 "offline test subprocess", "PowerShell asset", "explorer", "completion", "host groups", "Kerberos imports and DNS"]}))
    return 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--plugin-cli"]:
        from remoteops.commands.plugin_cli import main as plugin_main
        return plugin_main(args[1:])
    if args == ["--self-test"]:
        return self_test()
    if args:
        print("Usage: WMIBench [--self-test | --plugin-cli ...]", file=sys.stderr)
        return 2
    from remoteops.ui.app import run
    return run()
