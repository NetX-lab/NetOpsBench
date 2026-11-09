"""Small CONNECT relay: only explicitly allowed public OpenAI endpoints."""

import base64
import http.client
import ipaddress
import os
import select
import socket
import socketserver
import threading
from contextlib import contextmanager
from urllib.parse import urlsplit

ALLOWED_HOSTS = frozenset({"chatgpt.com", "api.openai.com", "auth.openai.com"})


def validate_destination(authority):
    host, port = authority.rsplit(":", 1)
    if host not in ALLOWED_HOSTS or port != "443":
        raise ValueError("Destination denied")
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("Non-public destination denied")
    return host, addresses[0]


class ConnectHandler(socketserver.StreamRequestHandler):
    def handle(self):
        connection = None
        try:
            self.connection.settimeout(30)
            request = self.rfile.readline(8192).decode("ascii").strip().split()
            if len(request) != 3 or request[0] != "CONNECT":
                raise ValueError("Only CONNECT is supported")
            host, address = validate_destination(request[1])
            for _ in range(100):
                if self.rfile.readline(8192) in (b"\r\n", b"\n", b""):
                    break
            upstream = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            if upstream:
                parsed = urlsplit(upstream)
                if parsed.scheme not in {"http", "https"}:
                    raise ValueError("Unsupported upstream proxy")
                cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
                connection = cls(parsed.hostname, parsed.port, timeout=30)
                headers = {}
                if parsed.username:
                    credential = base64.b64encode(f"{parsed.username}:{parsed.password or ''}".encode()).decode()
                    headers["Proxy-Authorization"] = f"Basic {credential}"
                connection.set_tunnel(host, 443, headers=headers)
                connection.connect()
                remote = connection.sock
            else:
                remote = socket.socket(address[0], address[1], address[2])
                remote.settimeout(30)
                remote.connect(address[4])
                connection = remote
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.wfile.flush()
            while True:
                readable, _, _ = select.select([self.connection, remote], [], [], 60)
                if not readable:
                    break
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (remote if source is self.connection else self.connection).sendall(data)
        except (ValueError, OSError, http.client.HTTPException):
            try:
                self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
        finally:
            if connection:
                connection.close()


@contextmanager
def openai_proxy(host):
    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = Server((host, 0), ConnectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
