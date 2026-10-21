"""One HTTP request for the outside services (Gmail, Claude), with every failure to get an answer folded into
status 0 so each caller's retry rule sees it."""

import http.client
import urllib.error
import urllib.request


def send(method: str, url: str, body: bytes | None, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request: urllib.request.Request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status: int = response.status
            return status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        return 0, b""  # incl. a connection dropped mid-answer (IncompleteRead, SSL errors): no response
