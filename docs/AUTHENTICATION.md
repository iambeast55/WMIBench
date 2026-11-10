# Authentication

The credential editor offers five modes for both WMI and WinRM:

| Mode | Secret |
| --- | --- |
| Kerberos / Password | Account password |
| Kerberos / RC4 key | 32 hexadecimal characters |
| Kerberos / AES key | 32 characters for AES-128, 64 for AES-256 |
| NTLM / Password | Account password |
| NTLM / Password hash | 32 hexadecimal characters: the NT hash, without an LM prefix |

Saved secrets are encrypted with the same database/key arrangement as existing passwords.
Leave the secret blank when editing to retain it. Changing secret type requires a new value.
Turning off **Save in database** prompts for the secret when connecting.

## Kerberos setup

In **Credentials**, set:

- **Username:** `user`, `DOMAIN\user`, or `user@example.com`.
- **Domain:** the AD DNS domain, such as `example.com`.
- **DC IP:** the domain controller to contact for tickets.
- **DNS server IP:** optional; defaults to the DC IP.

In the host editor, use either:

- **Address:** `server.example.com`, or a short name such as `server`. Short names
  are expanded using the profile's domain and resolved through the configured DNS server.
- **Address:** `192.0.2.10` and **Kerberos hostname:** `server.example.com`. This skips
  target DNS lookup while retaining the correct service identity. No reverse-DNS guess is made.

WMI requests a `host/server.example.com` service ticket; WinRM requests
`HTTP/server.example.com`. The hostname must match the computer's registered service
identity. A UPN suffix must match the configured domain; alternate UPN suffixes and
cross-realm referral workflows are not included in this preview.

Ticket requests go directly to the DC over TCP 88. DNS uses the selected server's
normal DNS service. The client does not edit hosts files, system DNS, Kerberos
configuration, or the logged-in user's ticket cache. Each host worker owns its tickets
and authentication state. Explicit domain connections use a direct network connection,
without an HTTP proxy. HTTPS retains certificate verification and uses the hostname
for SNI/certificate matching, even when the TCP connection goes to an explicit IP.

Kerberos errors do not trigger NTLM fallback. The DC/account must permit the selected
key type. Existing profiles without an explicit DC retain the previous system Kerberos
path until edited. Basic authentication is retained only for existing profiles.

## Transport coverage

The explicit-DC Kerberos code is shared by Windows and Linux. It uses Python protocol
libraries, without requiring a domain join or a separate system `kinit` installation.
WMI key/hash authentication uses the DCOM adapter on Windows as well as Linux.
WinRM uses the existing PSRP runspace with a connection-local authentication and DNS adapter.

Class metadata inspection in Explorer currently requires WinRM or native Windows WMI.
The DCOM adapter supports namespace browsing and the existing query/method/plugin commands,
but not class metadata enumeration. Switching credential type does not silently switch
between WMI and WinRM.

## Verification status

Offline fixtures cover password/hash selection, encrypted persistence, target resolution,
Kerberos AP replies, channel binding, AES/RC4 message integrity, sequence checking,
RPC bounds, and keeping TLS hostname verification separate from the TCP address.
The existing command/session tests remain in place.

Live Kerberos against an AD domain has not been tested in this workspace, nor has the
new code been run on Linux here. The Windows package is a preview for that validation.
The Linux executable must still be built on Linux.

For a first target check, connect and run `sysinfo` using each credential mode you need,
then run a second query on the same connection. Test WMI and WinRM separately. Check
that a bad credential fails without retrying under another method, and that an ordinary
command error leaves the healthy connection available.

Protocol references: [Microsoft RPC security](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rpce/52d5d675-8d9c-4e02-b3fc-7c0eacf41838),
[Kerberos GSS tokens](https://www.rfc-editor.org/rfc/rfc4121),
[RC4 Kerberos framing](https://www.rfc-editor.org/rfc/rfc4757).
