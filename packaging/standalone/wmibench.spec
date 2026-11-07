from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
hidden = collect_submodules("remoteops") + collect_submodules("keyring.backends")
hidden += ["unittest.mock", "xml.etree.ElementTree", "decimal", "statistics", "ipaddress"]
hidden += collect_submodules("impacket")
hidden += collect_submodules("minikerberos")
hidden += collect_submodules("asyauth")
hidden += collect_submodules("asysocks")
hidden += collect_submodules("unicrypto")
# unicrypto selects its crypto backend with importlib. PyInstaller cannot
# discover the pycryptodomex package by importing it during analysis because
# that backend intentionally exposes modules with the same names as its API.
hidden += [
    "unicrypto.backends.pycryptodomex",
    "unicrypto.backends.pycryptodomex.AES",
    "unicrypto.backends.pycryptodomex.DES",
    "unicrypto.backends.pycryptodomex.RC4",
    "unicrypto.backends.pycryptodomex.TDES",
    "unicrypto.backends.pycryptodome",
    "unicrypto.backends.pycryptodome.AES",
    "unicrypto.backends.pycryptodome.DES",
    "unicrypto.backends.pycryptodome.RC4",
    "unicrypto.backends.pycryptodome.TDES",
]
if sys.platform == "win32":
    hidden += ["win32timezone", "win32crypt", "pythoncom", "pywintypes", "win32com.client"]
data = collect_data_files("qtawesome")
for distribution in ("PySide6", "QtAwesome", "keyring", "cryptography", "pypsrp", "pyspnego", "impacket", "minikerberos", "asyauth", "asysocks", "unicrypto", "asn1crypto", "oscrypto", "dnspython"):
    data += copy_metadata(distribution, recursive=True)
if sys.platform == "win32":
    data += copy_metadata("pywin32")
data += [(str(root / "src/remoteops/remoting/terminal_sessions.ps1"), "remoteops/remoting")]

a = Analysis(
    [str(root / "packaging/standalone/launcher.py")],
    pathex=[str(root / "src")],
    binaries=[], datas=data, hiddenimports=hidden,
    hookspath=[], runtime_hooks=[],
    excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
              "PySide6.QtDataVisualization", "tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="WMIBench" if sys.platform == "win32" else "wmibench",
    debug=False, strip=False, upx=False,
    # Preserve pipes for offline test output, but hide an owned Windows console.
    console=True, hide_console="hide-early",
)
