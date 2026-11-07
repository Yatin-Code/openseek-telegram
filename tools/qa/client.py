"""QA runner HTTP surface (P1, issue #42).

`open_connection` speaks HTTPS against a target with EXACTLY the trust model the
product documents (docs/api/CONTRACT-v1.md "Transport"): a self-signed node is
its own CA and the SPKI pin is the identity. The pin is verified before any
application data flows; a mismatch aborts the run instead of being ignored.
"""
from __future__ import annotations

import socket
import ssl

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from openseek.tls import spki_pin_from_cert_der  # noqa: E402


def open_connection(host: str, port: int, pin: str = "", *, timeout: float = 15.0) -> socket:
    """TLS-connect while verifying the SPKI pin (never trusting an unverified CA)."""
    ctx = ssl.create_default_context()
    if pin:
        # Pin-only trust: the pin is the identity (the pairing contract's client side).
        # The pin check happens right after the handshake and before any request
        # bytes are sent; on mismatch the connection is closed and the run fails.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    conn = socket.create_connection((host, port), timeout=timeout)
    try:
        sock = ctx.wrap_socket(conn, server_hostname=host)
        if pin:
            got = spki_pin_from_cert_der(sock.getpeercert(True))
            if got != pin:
                sock.close()
                raise SystemExit(f"TLS pin mismatch: expected {pin}, got sha256//{got}")
        return sock
    except Exception:
        conn.close()
        raise


class HTTP:
    """HTTPS client returning (status, headers, body) with the pin as the only trust."""

    def __init__(self, base_url: str, pin: str = "", *, timeout: float = 15.0):
        import urllib.parse
        self.cached_base = urllib.parse.urlsplit(base_url)
        self.pin = pin
        self.timeout = timeout

    def request(self, method: str, path: str, *, headers: dict | None = None,
                data: bytes = b"", content_length: int | None = None,
                timeout: float | None = None) -> tuple[int, dict[str, str], bytes]:
        s = self.cached_base
        host, port = s.hostname, s.port or 443
        request_timeout = self.timeout if timeout is None else timeout
        declared_length = len(data) if content_length is None else content_length
        if declared_length < len(data):
            raise ValueError("Content-Length cannot be smaller than the request body")
        conn = open_connection(host, port, self.pin, timeout=request_timeout)
        try:
            head = bytearray()
            head += f"{method} {s.path.rstrip('/')}{path} HTTP/1.1\r\n".encode()
            head += f"Host: {host}\r\n".encode()
            head += b"Connection: close\r\n"
            for name, value in (headers or {}).items():
                if name.lower() == "content-length":
                    continue
                head += f"{name}: {value}\r\n".encode()
            head += f"Content-Length: {declared_length}\r\n\r\n".encode()
            # Send headers and body separately. A server that rejects from headers
            # can reply while an upload is in progress; keep that early response
            # available to callers instead of turning it into a sendall reset.
            try:
                conn.sendall(bytes(head))
                if data:
                    conn.sendall(data)
            except (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError,
                    ssl.SSLZeroReturnError):
                pass
            response = b""
            while True:
                try:
                    chunk = conn.recv(65536)
                except (socket.timeout, ConnectionResetError):
                    break
                if not chunk:
                    break
                response += chunk
        finally:
            conn.close()
        head, _, body = response.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split()[1]) if len(lines) >= 1 and len(lines[0].split()) >= 2 else 0
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        return status, headers, body

    def get(self, path: str, headers: dict | None = None):
        return self.request("GET", path, headers=headers)

    def post(self, path: str, headers: dict | None = None, data: bytes = b""):
        return self.request("POST", path, headers=headers, data=data)

    def put(self, path: str, headers: dict | None = None, data: bytes = b""):
        return self.request("PUT", path, headers=headers, data=data)


# ------------------------------------------------------------------ evidence
class Steps:
    """Collects one row per step: (name, passed, evidence lines). Witness data is
    scrubbed once, at print time, by secrecy.guard()."""

    def __init__(self):
        self.rows: list[tuple[str, bool, list[str]]] = []
        self._current: list[str] = []

    def evidence(self, line: str) -> None:
        self._current.append(line)

    def check(self, name: str, passed: bool) -> None:
        self.rows.append((name, bool(passed), list(self._current)))
        self._current = []
