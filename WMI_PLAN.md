# Optional WMI Connection Mode

## Build-First Direction - September 22, 2026

The user requested continued development with lab testing deferred until more
features are available. This supersedes the earlier requirement to finish each live
gate before building the next part. Part 4b, session reuse (5), and initial Linux
`sysinfo` integration (6) are implemented in source but remain experimentally
validated by local fixtures only, except for the earlier native object-codec smoke.
Do not describe Linux query/session interoperability as verified. The user later
requested the current work packaged for testing: `dist/remoteops-linux-wmi-preview.tar.gz`.
SSH, Ubuntu deployment, and live testing have not been performed.

## Custom Linux Client Milestones - September 20, 2026

Chosen direction: implement our own Python RPC/DCOM/WMI protocol client. No
Impacket, other WMI protocol client dependency, SMB, WinRM fallback, or Windows
relay. Established authentication/cryptography primitives may be used later;
we will not invent cryptography. Existing Windows COM and PSRP adapters stay intact.
This sequence supersedes the historical delivery sequence below.

1. **RPC connection probe - implemented and verified on Windows and Linux.** The standalone
   `remoteops.remoting.rpc_probe` module builds an unauthenticated endpoint-mapper
   v3.0 / NDR v2.0 bind, checks the response, and closes the TCP socket without
   sending RPC requests. Numeric IPs only; default port 135. It uses a total
   deadline, bounded responses, little-/big-endian response decoding, and explicit
   errors for rejection, malformed/unsupported packets, EOF, and network failures.
   No credentials, WMI operations, or GUI changes. All 62 tests pass (19 new probe
   tests). A real local Windows endpoint-mapper bind passed. All 19 probe tests
   also passed on Ubuntu 24.04.4 with Python 3.12.3, followed by a successful bind
   from Ubuntu to the approved remote Windows host's TCP port 135. The server
   accepted endpoint mapper v3.0 / NDR v2.0. No Windows credentials were used.
   This gate is complete; it does not itself test authentication or WMI readiness.
2. **Authenticated RPC - NTLM subpart implemented and live-verified.** The standalone
   `remoteops.remoting.rpc_auth_probe` performs NTLMv2 over TCP, requires packet
   privacy and header signing, and validates a protected `IObjectExporter.ServerAlive`
   reply before reporting success. This read-only native RPC call neither activates
   objects nor queries WMI. Our code owns RPC framing, security trailers, deadlines,
   and response validation; pyspnego 0.12.2 supplies NTLM and signing/sealing.
   A successful run from Ubuntu to the approved Windows lab host returned status 0.
   One nonexistent-account check returned unverified access-denied fault 0x00000005,
   correctly treated as failure. Real bad-password/tampering/replay/downgrade tests
   use local synthetic NTLM fixtures, not repeated failures on a real lab account.
   No server policy or account settings were changed. There is no authentication
   downgrade or automatic retry. Kerberos is a separate, unimplemented subpart;
   domain NTLM and server-side NTLM-disabled policy still need live validation.
   Final regression counts: 80 tests on Windows, 37 RPC-focused tests on Ubuntu.
3. **DCOM object connection - implemented and live-verified from Ubuntu.**
   `remoteops.remoting.dcom_probe` discovers the DCOM version with unauthenticated
   ServerAlive2, then attempts protected IActivation.RemoteActivation for only
   IWbemLevel1Login. Our bounded NDR32 codecs decode standard object references,
   interface status, and returned TCP bindings. On success the probe uses the same
   approved numeric target IP and returned exporter port, authenticates to
   IRemUnknown, and releases the exact acquired public references. All sockets close;
   cleanup has a separate five-second allowance and no automatic retry. Cancellation
   and progress-output failures still attempt release when a reference is known.
   Malformed/lost activation replies explicitly leave cleanup unconfirmed.
   No namespace login, query, session keepalive, GUI change, or installer rebuild.
   Unsupported fragmentation, nonstandard OBJREFs, and ORPC extensions fail explicitly.
   Validation: 111 tests pass on Windows and 68 protocol-focused tests pass on
   Ubuntu 24.04.4 / Python 3.12.3 / pyspnego 0.12.2. The decoder now ignores arbitrary
   ORPCTHAT flags as required by MS-DCOM 2.2.13.4; non-null extensions remain explicitly
   unsupported, with separate diagnostics. This fixes the reported ORPCTHAT rejection.
   At 2026-09-22 00:34:58 UTC (September 21, 20:34:58 EDT), the live probe obtained
   IWbemLevel1Login, reached the same Windows host's exporter on TCP 49678, and
   verified protected RemRelease. DCOM version 5.7. This completes the part 3 lab gate.
   Earlier access-denied attempts are historical; their cause was not established,
   and this flags fix does not itself explain those faults. No target policies were
   changed by the agent. Namespace access, queries, and persistent sessions remain
   untested. The standalone diagnostic stays installed in the Ubuntu user's home.
4. **One WMI query - 4a complete; 4b implemented with live gate deferred.**
   The existing diagnostic now accepts `--namespace root/cimv2`. It uses protected
   IWbemLevel1Login.NTLMLogin opnum 6, validates the returned IWbemServices identity,
   then releases both public-reference counts. No namespace is created or modified.
   Live Ubuntu-to-Windows login and release passed at 2026-09-22 00:43:25 UTC.
   A nonexistent namespace returned 0x8004100e at 00:43:52 UTC, with exit code 1 and
   login-reference release confirmed. 128 Windows tests and 85 Ubuntu protocol tests
   pass. Denied namespace access, malformed replies, and cancellation use fixtures.
   The standalone Ubuntu installation is updated; GUI and installer are unchanged.
   Cross-exporter namespace references remain unsupported and report uncertain cleanup
   rather than being released through the wrong exporter.
   Part 4b now adds `--query-os`, a fixed seven-property Win32_OperatingSystem SELECT
   in root/cimv2, semisynchronous/forward-only enumeration, and structured JSON after
   all three reference counts are released. A bounded WMIO decoder handles the
   projected string/datetime/uint64 values, nulls, Unicode, and class defaults.
   Protected reply fragmentation is supported up to 1 MiB / 512 fragments, with
   each fragment verified independently; requests remain single-fragment.
   151 local tests pass, and a Windows native-marshaler smoke test decoded a
   1356-byte OS object to seven values matching native WMI. This does not establish
   remote query interoperability. SSH, Ubuntu deployment, and live query testing
   are deferred at the user's request. The Ubuntu diagnostic still has part 4a.
   The user subsequently approved continuing implementation before this live gate.
5. **Session reuse - implemented, live validation deferred.** `DcomClient` retains
   authenticated resolver, services, enumerator, and release associations and the
   namespace reference. Each query releases its temporary enumerator. ComplexPing
   registers/removes unique OIDs and SimplePing maintains their lifetime, honoring
   SORF_NOPING. Queries, pings, and cleanup share the host worker. Protocol pings
   remain active independently of optional idle health checks. Deadlines are bounded;
   errors require explicit reconnect. No automatic retries. Cleanup uncertainty is
   reported and all local sockets close, including after partial initialization.
6. **WMIBench integration - initial sysinfo implemented, live validation deferred.**
   Non-Windows clients select `DcomWmiSession`; Windows retains native COM. Only
   built-in `sysinfo` is enabled on Linux, with NTLM, numeric IP, and root/cimv2.
   Other commands fail before credential resolution/network dispatch. Connected
   requires namespace access and a successful complete query. Results flow into the
   existing tables/history and session states. Local Qt checks cover repeated runs,
   tables/history, command rejection, and disconnect; Linux rendering/responsiveness
   and real remote lifetime behavior still require later validation.
7. **Incremental commands.** Add read-only aliases individually, then native WMI
   management methods in separate work packages. Each needs validation and a real
   host test. File transfer and arbitrary process execution are outside this plan.

The first useful end-to-end target is: connect once, run `sysinfo` repeatedly,
disconnect cleanly. A reusable WMI connection is not a PowerShell runspace.

The standalone probes remain separate from the GUI session backend. Both can run
from the source tree as described in README.md; the distributable has not been rebuilt.
Do not interpret a successful unauthenticated bind as WMI readiness,
or a rejected unauthenticated bind as proof that authenticated WMI cannot work.

Protocol references for the application-owned implementation:

- [C706 RPC specification, chapter 12](https://pubs.opengroup.org/onlinepubs/9629399/chap12.htm)
- [MS-RPCE presentation context negotiation](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rpce/ca4d3552-4647-4f40-830b-fd2090adec8f)
- [MS-RPCE bind rejection extensions](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rpce/92ba4942-0b1f-41aa-8924-69dd6e49b546)
- [MS-DCOM layering and object lifecycle](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dcom/86b9cf84-df2e-4f0b-ac22-1b957627e1ca)
- [MS-RPCE AUTH3 layout](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rpce/a6b7b03c-4ac5-4c25-8c52-f2bec872ac97)
- [MS-RPCE header signing](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rpce/4886f349-2a73-4f9e-9262-a8404462c7e9)
- [MS-RPCE verification trailer](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rpce/0e9fea61-1bff-4478-9bfe-a3b6d8b64ac3)
- [MS-DCOM ServerAlive](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dcom/6d41d68a-ee69-4439-818c-595836983356)
- [MS-DCOM ServerAlive2](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dcom/c898afd6-b75d-4641-a2cd-b50cb9f5556d)
- [MS-DCOM RemoteActivation](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dcom/cf56f36e-a87e-49b4-9729-ebb8a07054fb)
- [MS-DCOM interface and structure IDL](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dcom/49aef5a4-f0ad-4478-abb5-cb9446dc13c6)
- [MS-DCOM ORPCTHAT flags and extensions](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dcom/10ffacb5-c8b2-4b6a-bccc-9e515818661f)
- [MS-WMI interface identifiers](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmi/3485541f-6950-4e6d-98cb-1ed4bb143441)
- [MS-WMI NTLMLogin](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmi/40d194a2-c28a-485b-97f6-11a7c08f147e)

## Historical Implementation - September 18, 2026

The user has specified **WMI/DCOM only (no SMB)** and **no Impacket**. These constraints
supersede the optional SMB/Impacket proposals retained below as historical planning.

WMIBench 0.2.0 has an application-owned WMI adapter using Windows' native COM
interfaces through pywin32. It does not invoke Impacket, PowerShell, SMB, or WinRM.
It supports native queries for sysinfo, disk, services, svc, procs, hotfixes, net,
shares, users, groups, startup, and uptime. Provider fields can differ from PSRP.
Other aliases fail locally before authentication. WMI transfers are disabled.

Windows COM connections are retained on a dedicated host thread with idle checks.
Query results feed the same tables and history as PSRP. Transport errors require an
explicit reconnect; command dispatch never silently switches protocol.

Direct Linux WMI/DCOM is **not implemented**. It requires an application-owned
DCERPC/DCOM/WMI transport, authentication integration, object marshaling, and remote
interoperability testing. The Linux build reports this limitation explicitly rather
than pretending native Windows COM is portable. WinRM works on both platforms.

A local native Windows WMI query has passed. Remote domain authentication, firewall
behavior, and enterprise WMI providers still require testing against permitted hosts.

Planning document, September 13, 2026. No implementation or package changes yet.

## Objective and Scope

Allow each host to use either WinRM/PSRP or WMI over DCOM from the same Modern Ops
Console on Windows and Linux. Preserve approved command aliases, local help,
searchable history, structured tables, and file-transfer controls.

The important distinction is "without WinRM" versus "only WMI traffic."
Native WMI queries and management methods can work directly over DCOM. General
process output capture and practical file transfers need a separate data channel
in the proposed design: SMB. Thus full functionality is planned as WMI + SMB;
strict WMI-only hosts receive the capabilities available through native providers.
Do not promise universal feature parity when SMB or necessary providers are absent.

## 1. Per-Host Connection Settings

- Add `connection_mode`: `winrm` or `wmi_dcom`. Keep the existing HTTP/HTTPS
  transport setting specific to WinRM, rather than overloading it with WMI.
- Migrate existing hosts to `winrm` without changing their saved settings.
- Expose the mode in Add/Edit Host, with only relevant connection fields visible.
- WMI settings include credential profile, namespace (initially `root/cimv2`),
  timeouts, and optional SMB transfer/output configuration.
- Show the selected mode and tested capabilities in the existing host tab.
  Changing modes requires disconnecting first. Never silently switch protocols.

## 2. Cross-Platform Backend

- Keep Python and PySide6. Evaluate Impacket's DCOM/WMI and SMB library APIs as
  the backend, using its source examples as references rather than invoking a
  command-line tool for each operation.
- Prototype Linux and Windows authentication before committing to a supported,
  pinned dependency version. Include the user's Kali/Python version in testing.
- Support Kerberos and NTLM profiles as permitted by the environment; WinRM Basic
  authentication does not map to this backend. Do not silently downgrade auth.
- Add a WMI session adapter under `src/remoteops/remoting`, selected by the session
  manager. Include connection mode in session identity.
- Keep library objects on their owning worker thread; serialize per-host work and
  send UI results through queued Qt signals.
- Separate native query/method operations from optional process execution and SMB
  transfers. Expose capability flags rather than assuming every host permits all three.

## 3. Persistent Connection and Status

- Retain authenticated DCOM/WMI handles and an optional SMB connection across
  commands, with bounded, idle-only health checks and controlled reconnects.
- Mark WMI connected only after authentication, namespace access, and a small
  read-only query succeed. Report SMB readiness separately.
- Include Connecting, Connected, Reconnecting, Disconnecting, and Error states;
  keep disabled features accompanied by their actual capability failure reason.
- Reusing WMI connections does not create a persistent PowerShell runspace.
  Processes launched for individual commands do not share PowerShell variables.
  Describe this distinction honestly; true remote shell-state persistence would
  require a separately designed long-lived runner and is outside this first phase.
- Never automatically retry a modifying operation after an ambiguous connection
  failure. Preserve an unknown outcome for history and user inspection.

## 4. One Approved Command Catalog, Multiple Implementations

- Extend command metadata with supported backends, argument schema, result columns,
  required capabilities, and backend-specific operations.
- Keep alias names, argument validation, and `-h` consistent across modes. Help
  remains local and states backend-specific limitations.
- Implement native WMI query/method variants first: system information, disks,
  processes, services, uptime, and network configuration where providers support it.
- Return WMI property values directly as structured records for table display.
- Audit every existing alias. Mark each as native WMI, approved process plus SMB,
  or unavailable. Do not substitute different semantics without documenting them.
- For approved commands requiring PowerShell, optionally launch the registered
  implementation on the Windows target through WMI. No local PowerShell or WinRM
  is needed, but target PowerShell and permission to create processes are required.
- Reject unknown aliases and unrecognized arguments locally. Construct typed WMI
  operations and safe parameter values; do not expose raw WQL or shell input as a
  fallback. History reruns pass through the same validation.

## 5. Output, Transfers, and History

- Native queries return records; native management methods return their method
  status and relevant properties. Normalize these alongside PSRP results.
- WMI process creation returns a creation result and process ID, not a stream of
  stdout or a final command exit code. Do not label process creation as successful
  command completion.
- For process-based commands, design a registered runner that writes structured
  results, stdout, stderr, and a completion record to a protected temporary area.
  Retrieve through an approved SMB share, using unique per-operation paths and
  cleanup limited to files owned by that operation.
- Use bounded polling, explicit encoding, output-size limits, and an actual completion
  record. Missing output or a timeout must not be interpreted as command success.
- Use SMB for upload/download, with configured shares and remote paths, progress,
  cancellation, overwrite handling, and verification. Do not assume `ADMIN$` is
  available. SMB failure must not block native WMI queries.
- Store mode, alias, arguments, status, timing, records, and full textual output in
  the shared searchable history. Keep secrets out of records.

## 6. Environment Checks and Validation

- Diagnose RPC endpoint mapper access (TCP 135), the negotiated WMI/DCOM endpoint,
  namespace permissions, DCOM authentication requirements, and credentials.
- Check SMB (normally TCP 445), share access, and any configured output directory
  only for capabilities that require them. Opening port 135 alone is insufficient.
- Report prerequisites rather than automatically modifying firewall, UAC, DCOM, or
  authentication policy on managed hosts.
- Test with WinRM unavailable on the target, from both Linux and Windows clients.
- Cover WMI-only access, WMI plus SMB, SMB denied, wrong credentials, query-only
  permissions, idle expiry, dropped connections, and explicit disconnect.
- Test rejected aliases without network dispatch, literal argument escaping,
  structured tables, complete errors, transfer interruption, and output cleanup.
- Verify history and UI status remain correct when only part of a host's capabilities
  are available. Rebuild the Linux distribution after implementation and testing.

## Delivery Sequence

1. Prototype authenticated native queries from both client platforms, with WinRM off.
2. Add persisted host mode, session adapter, capability checks, and honest status.
3. Port the native command subset with shared help, validation, tables, and history.
4. Add optional SMB output capture and file transfers; test remaining command parity.
5. Add idle health checks and recovery tests, then package the supported backend.

Before implementing full-feature parity, establish whether the affected hosts also
allow SMB. If they permit strictly WMI/DCOM only, deliver the native subset first
and document unsupported operations rather than silently introducing another channel.

## Technical References

- [Microsoft: Remote WMI connection prerequisites](https://learn.microsoft.com/en-us/windows/win32/wmisdk/connecting-to-wmi-remotely-starting-with-vista)
- [Microsoft: Win32_Process.Create inputs and results](https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/create-method-in-class-win32-process)
- [Microsoft: Creating processes remotely through WMI](https://learn.microsoft.com/en-us/windows/win32/wmisdk/creating-processes-remotely)
- [Fortra: Impacket WMI execution reference, including SMB output retrieval](https://github.com/fortra/impacket/blob/master/examples/wmiexec.py)

The implementation choices above are proposed WMIBench architecture, not a claim
that the reference tools provide the complete application behavior described here.
