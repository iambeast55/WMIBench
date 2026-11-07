# PowerShell parameter converter

`tools/ps_params_to_schema.py` converts a common PowerShell `param()` block
into the `parameters` object used by a WMIBench command. It is an offline tool;
it does not run the script or copy it to a remote host.

```powershell
python tools/ps_params_to_schema.py .\inventory.ps1
```

Example input:

```powershell
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$ComputerName,
    [int]$Depth = 2,
    [switch]$Force,
    [ValidateSet('Fast', 'Safe')]
    [string]$Mode = 'Safe'
)
```

The output is ready to paste into `command.json`:

```json
{
  "_schema": {
    "ComputerName": {"type": "str", "required": true},
    "Depth": {"type": "int", "default": 2},
    "Force": {"type": "bool", "default": false},
    "Mode": {"type": "str", "default": "Safe", "choices": ["Fast", "Safe"]}
  },
  "_positional": ["ComputerName"]
}
```

Paste that object under the command's `parameters` key. The converter maps
common scalar PowerShell types to `str`, `int`, `float`, and `bool`. Switches
become Boolean arguments with a default of `false`; WMIBench currently expects
`--force true` or `--force false`, rather than a bare `--force`.

PowerShell arrays, computed defaults, custom validation attributes, and rich
objects are not representable by the current WMIBench schema. The converter
keeps the command usable where possible and reports anything it
could not map exactly. Arrays and complex values should generally be accepted
as strings and parsed by the PowerShell wrapper.
