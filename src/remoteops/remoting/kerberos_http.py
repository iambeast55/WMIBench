"""WinRM Kerberos authentication with a connection-local address mapping."""
from urllib.parse import urlsplit

from pypsrp.negotiate import HTTPNegotiateAuth
from pypsrp._pool_manager import DisableNewConnectionsContext, NewConnectionDisabled
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.util.connection import create_connection

from remoteops.remoting.kerberos import KerberosContext, KerberosFailure


class KerberosHttpAuth(HTTPNegotiateAuth):
    def __init__(self, provider):
        super().__init__(auth_provider="kerberos")
        self.provider = provider

    def handle_401(self, response, **kwargs):
        scheme = kwargs.pop("_pypsrp_auth_provider")
        if scheme.lower() not in ("negotiate", "kerberos"):
            raise KerberosFailure("The WinRM server did not offer Kerberos authentication.")
        host = urlsplit(response.url).hostname
        if host != self.provider.hostname or host in self.contexts:
            raise KerberosFailure("WinRM rejected the existing Kerberos connection. Reconnect explicitly.")
        binding = self._get_cbt_data(response) if response.url.startswith("https:") else None
        if response.url.startswith("https:") and binding is None:
            raise KerberosFailure("Could not read the WinRM TLS certificate for channel binding.")
        context = KerberosContext(self.provider, "HTTP", channel_binding=binding,
                                  negotiate=scheme.lower() == "negotiate")
        self.contexts[host] = context
        token = context.step()
        response.content
        response.raw.release_conn()
        request = response.request.copy()
        self._set_auth_token(request, token, scheme)
        response = response.connection.send(request, **kwargs)
        if response.status_code == 401:
            raise KerberosFailure("WinRM rejected the Kerberos credential or service identity.")
        token = self._get_auth_token(response, self._regex)
        context.step(token)
        context.response_auth_header = scheme.lower()
        return response


class MappedAdapter(HTTPAdapter):
    def __init__(self, hostname, address):
        self.hostname, self.address = hostname, address
        super().__init__(max_retries=0)

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        super().init_poolmanager(connections, maxsize, block=block, **kwargs)
        address, expected = self.address, self.hostname

        class MappedConnection:
            def _new_conn(self):
                if DisableNewConnectionsContext.current() is not None:
                    raise NewConnectionDisabled()
                if self.host != expected:
                    raise KerberosFailure("WinRM attempted to connect to a different host.")
                return create_connection((address, self.port), self.timeout,
                                         self.source_address, self.socket_options)

        class PlainConnection(MappedConnection, HTTPConnection):
            pass

        class TlsConnection(MappedConnection, HTTPSConnection):
            pass

        class PlainPool(HTTPConnectionPool):
            ConnectionCls = PlainConnection

        class TlsPool(HTTPSConnectionPool):
            ConnectionCls = TlsConnection

        self.poolmanager.pool_classes_by_scheme = {"http": PlainPool, "https": TlsPool}

    def proxy_manager_for(self, proxy, **kwargs):
        raise KerberosFailure("Explicit domain connections require a direct connection to the host.")


def configure(wsman, provider):
    session = wsman.transport._build_session()
    session.trust_env = False
    session.proxies.clear()
    session.auth = KerberosHttpAuth(provider)
    for scheme in ("http://", "https://"):
        old = session.adapters.get(scheme)
        session.mount(scheme, MappedAdapter(provider.hostname, provider.address))
        if old:
            old.close()
    wsman.transport.session = session
