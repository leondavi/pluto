"""Python SDK for the Pluto coordination server.

Modules:
    tcp_client      :class:`PlutoClient` — persistent TCP session with
                    pushed events delivered to handler callbacks.
    http_client     :class:`PlutoHttpClient` — token-authenticated HTTP
                    session; messages are fetched by poll/peek.
    http_pool       :class:`HTTPConnectionPool` — keep-alive sockets
                    shared by one HTTP client.
    errors          :class:`PlutoError`.
    snapshot_files  :func:`write_snapshot_files` — on-disk snapshot layout.
    guide           :func:`generate_agent_guide`.
    cli             ``main()`` behind ``pluto_client.py`` / ``PlutoClient.sh``.

Wire-protocol constants (op names, statuses, event names, defaults) live
in the sibling module ``pluto_client_def``. Existing code that imports
from ``pluto_client`` keeps working; that module re-exports this API.
"""

from pluto_sdk.errors import PlutoError
from pluto_sdk.guide import generate_agent_guide
from pluto_sdk.http_client import PlutoHttpClient
from pluto_sdk.http_pool import HTTPConnectionPool
from pluto_sdk.snapshot_files import write_snapshot_files
from pluto_sdk.tcp_client import PlutoClient

__all__ = [
    "HTTPConnectionPool",
    "PlutoClient",
    "PlutoError",
    "PlutoHttpClient",
    "generate_agent_guide",
    "write_snapshot_files",
]
