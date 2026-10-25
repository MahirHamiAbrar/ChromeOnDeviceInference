"""A minimal, dependency-free Chrome DevTools Protocol client.

Chrome exposes CDP over a WebSocket
(``ws://127.0.0.1:<port>/devtools/page/<target-id>``).  Rather than depend on a
WebSocket library, this module implements just enough of RFC 6455 to send CDP
commands and read their replies: a client-side masked text frame writer and a
frame reader that ignores unsolicited events.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct

# WebSocket opcodes (RFC 6455 section 5.2).
_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class CDPError(RuntimeError):
    """Raised when the browser rejects a CDP command."""


class CDPClient:
    """Synchronous Chrome DevTools Protocol client.

    Commands are JSON objects with a monotonically increasing ``id``; replies
    echo that id.  Protocol *events* (which have no id) are discarded, which is
    all the Prompt API workflow needs.
    """

    def __init__(self, url: str, timeout: float | None = None):
        if not url.startswith("ws://"):
            raise ValueError(f"only ws:// URLs are supported, got {url!r}")
        hostport, _, path = url[5:].partition("/")
        path = "/" + path
        host, _, port = hostport.partition(":")
        port = int(port or 80)

        self._buf = b""
        self._next_id = 0
        self._sock = socket.create_connection((host, port), timeout=30)
        self._handshake(hostport, path)
        # Inference can take minutes; the caller owns the read timeout.
        self._sock.settimeout(timeout)

    # -- connection --------------------------------------------------------
    def _handshake(self, hostport: str, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(request.encode())

        # Note: we deliberately do NOT send an Origin header.  Chrome rejects
        # handshakes whose Origin is not in --remote-allow-origins, but accepts
        # non-browser clients that omit it entirely.
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake: connection closed")
            raw += chunk
        header, _, self._buf = raw.partition(b"\r\n\r\n")
        if b" 101 " not in header.split(b"\r\n")[0] + b" ":
            raise ConnectionError(
                "WebSocket handshake rejected:\n" + header.decode("utf-8", "replace")
            )

    # -- framing -----------------------------------------------------------
    def _recv_exact(self, count: int) -> bytes:
        while len(self._buf) < count:
            chunk = self._sock.recv(1 << 16)
            if not chunk:
                raise ConnectionError("DevTools socket closed")
            self._buf += chunk
        data, self._buf = self._buf[:count], self._buf[count:]
        return data

    def _send_frame(self, payload: bytes, opcode: int = _OP_TEXT) -> None:
        # Client-to-server frames must be masked with a fresh random key.
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", length)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", length)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self._sock.sendall(header + mask + masked)

    def _recv_frame(self) -> tuple[int, bytes]:
        first, second = self._recv_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if (second & 0x80) else None
        payload = self._recv_exact(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    # -- protocol ----------------------------------------------------------
    def call(self, method: str, params: dict | None = None) -> dict:
        """Invoke a CDP command and return its ``result`` object."""
        self._next_id += 1
        message_id = self._next_id
        self._send_frame(
            json.dumps(
                {"id": message_id, "method": method, "params": params or {}}
            ).encode()
        )
        while True:
            opcode, payload = self._recv_frame()
            if opcode == _OP_CLOSE:
                raise ConnectionError("Chrome closed the DevTools connection")
            if opcode == _OP_PING:
                self._send_frame(payload, opcode=_OP_PONG)
                continue
            if opcode != _OP_TEXT:
                continue  # pong, or a continuation frame we do not need
            message = json.loads(payload.decode("utf-8"))
            if message.get("id") != message_id:
                continue  # an event, not our reply
            if "error" in message:
                raise CDPError(f"{method} failed: {message['error']}")
            return message.get("result", {})

    def evaluate(self, expression: str, user_gesture: bool = False):
        """Evaluate JS in the page and return the value by value.

        ``awaitPromise`` makes a returned promise resolve before we get the
        result; ``userGesture`` grants the transient activation some built-in AI
        entry points require.
        """
        params = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        }
        if user_gesture:
            params["userGesture"] = True
        result = self.call("Runtime.evaluate", params)
        if "exceptionDetails" in result:
            detail = json.dumps(result["exceptionDetails"])[:800]
            raise CDPError(f"evaluate threw: {detail}")
        return result.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self._send_frame(b"", opcode=_OP_CLOSE)
        except Exception:
            pass
        finally:
            try:
                self._sock.close()
            except Exception:
                pass

    def __enter__(self) -> "CDPClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
