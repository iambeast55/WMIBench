# WMI Explorer

Connect a host and open **Explorer**. It loads classes in the current namespace.
The namespace field accepts paths such as `root/cimv2` or `root/default`.
Use the parent arrow or the Namespaces list to navigate. Double-click a child namespace
to open it. Refresh reloads the active list.

## Inspect A Class

Select a class to load its properties and methods. The name filter searches the
loaded list locally. Class qualifiers are available in the class-name tooltip.

- **Properties:** name, CIM type, key flag, and qualifiers.
- **Methods:** select a method to see input/output parameters and their qualifiers.
  Static methods are marked in the selector. Method qualifiers are in its tooltip.
- **Instances:** query results, with filtering, cell copying, and CSV export.
- **Python:** an example based on the selected class or method.

Query uses selected property rows, or the first 64 supported properties when none
are selected. Embedded objects are displayed in the schema but excluded from these
queries and `ctx.query` examples. The row limit is 1-1,000; reaching it is shown
beside the row count. A limited query does not claim to contain every instance.

The copy buttons generate `ctx.query` or `ctx.invoke` examples. The latter does not
execute anything. Add input names to your plugin's parameter schema, and set
`modifies_remote` to `true` before using an invocation example. Instance methods
also need an `object_path` argument, built from the instance's keys with
`ctx.object_path`. Methods using embedded objects cannot be represented by the
current plugin API. See [API reference](PLUGIN_API.md).

## Backend Support

| Client and connection | Namespaces | Classes and methods | Instances |
| --- | --- | --- | --- |
| Windows, native WMI/DCOM (password, system authentication) | Yes | Yes | Yes |
| Windows or Linux, WinRM | Yes | Yes | Yes |
| WMI/DCOM (Linux; Windows with keys/hashes or explicit DC) | Yes | Not yet | Not through Explorer |

WinRM inspection uses CIM cmdlets on the target. The copied `ctx.query` and
`ctx.invoke` examples are for WMI plugins, even when the schema was inspected over
WinRM. There is no automatic transport fallback.

Explorer operations use the host's existing worker and connection. They are
read-only, recorded in History, bounded to 60 seconds, and not retried automatically.
Class/namespace lists stop with an error above 10,000 entries. Provider errors do
not disconnect an otherwise healthy session. Errors appear in Terminal.

## Console Completion

Suggestions come from the current command library, including automatically loaded
plugins. They include aliases, parameter flags, declared choices, and boolean values.
Commands unavailable for the selected connection type are excluded.

- Tab or Ctrl+Space opens suggestions.
- Tab or Enter accepts the highlighted suggestion without running it.
- Escape closes suggestions.
- Up/Down navigates suggestions while open, or command history while closed.

Quoted arguments, `--name=value`, `name=value`, and positional choices are supported.
Completion does not query the remote host or execute plugin code.

## Implementation References

- [SWbemServices.SubclassesOf](https://learn.microsoft.com/en-us/windows/win32/wmisdk/swbemservices-subclassesof)
- [Get-CimClass](https://learn.microsoft.com/en-us/powershell/module/cimcmdlets/get-cimclass)

Offline coverage includes input validation, metadata conversion, generated Python
and PowerShell syntax, row limits, session recovery, stale responses, and Qt keyboard
interaction. Live target acceptance remains separate from these checks.
