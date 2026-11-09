# Proxychains

Linux TCP connections use the process socket layer. Run WMIBench through
proxychains-ng:

```bash
proxychains4 wmibench
```

Configure `proxy_dns` in the proxychains configuration when using hostnames.
With proxychains active, WMIBench resolves target hostnames through the
intercepted `getaddrinfo` path instead of sending DNS directly to the profile's
DNS server. Numeric target addresses bypass DNS.

Kerberos uses TCP to the configured DC on port 88. WMI/DCOM also needs TCP 135
and the dynamic RPC endpoint returned by the endpoint mapper. The proxy must
permit all of those connections. WinRM needs the configured HTTP/HTTPS port.

The application does not read `HTTP_PROXY` or `ALL_PROXY`; proxychains is
process-level interception. `WMIBENCH_PROXYCHAINS=1` forces the proxy-aware DNS
path when the preload library name is not visible in `LD_PRELOAD`.
