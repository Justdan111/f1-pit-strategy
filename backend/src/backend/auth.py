"""API key checking for the WebSocket.

Keys may arrive two ways, because browsers cannot set headers on a WebSocket
handshake:

  Authorization: Bearer <key>          non-browser clients
  Sec-WebSocket-Protocol: f1key.<key>  browsers, via new WebSocket(url, [proto])

A query parameter is deliberately NOT accepted. Uvicorn's access log writes
the full path including the query string, so every connection would write a
live key to disk. The subprotocol header is not logged.

When a subprotocol is used the server must echo one back, or the browser
rejects the connection -- see `selected_subprotocol`.
"""

import secrets

SUBPROTOCOL_PREFIX = "f1key."


def extract_key(
    authorization: str | None,
    subprotocols: list[str] | None,
) -> str | None:
    """Pull the presented key out of whichever channel carried it."""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()

    for protocol in subprotocols or []:
        if protocol.startswith(SUBPROTOCOL_PREFIX):
            key = protocol[len(SUBPROTOCOL_PREFIX) :].strip()
            if key:
                return key

    return None


def is_authorised(presented: str | None, accepted: list[str]) -> bool:
    """Whether a presented key is one of the accepted ones.

    No keys configured means the check is disabled. compare_digest is used
    rather than `==` so the comparison time does not depend on how many
    leading characters matched, which would otherwise let a key be guessed
    one character at a time.
    """
    if not accepted:
        return True
    if not presented:
        return False
    return any(secrets.compare_digest(presented, key) for key in accepted)


def selected_subprotocol(subprotocols: list[str] | None) -> str | None:
    """The subprotocol to echo back on accept.

    A browser that offers a subprotocol closes the connection unless the
    server names one of the offered values in its response, so this must be
    echoed even though it carries no meaning beyond the key.
    """
    for protocol in subprotocols or []:
        if protocol.startswith(SUBPROTOCOL_PREFIX):
            return protocol
    return None
