"""Real local receivers for the notification tests: an HTTP server that records what is
POSTed to it, and an SMTP server that speaks enough of the protocol (EHLO, STARTTLS, implicit TLS,
AUTH PLAIN, MAIL, RCPT, DATA, QUIT) to take a message from `smtplib`. Neither is a fake of the code
under test: the channel code talks to a real socket and a real TLS handshake.

Every receiver binds 127.0.0.1 on an ephemeral port, runs on daemon threads, and is stopped by its
fixture. Nothing here ever connects out.
"""

from __future__ import annotations

import base64
import datetime
import ipaddress
import socket
import socketserver
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class WebhookReceiver:
    """Records every POST. `status` is the response code (a list is consumed one per request, the
    last one repeating), `delay` holds the response that long, `redirect_to` answers 302."""

    def __init__(self, status=200, delay: float = 0.0, redirect_to: str | None = None) -> None:
        self.requests: list[dict] = []
        self._statuses = list(status) if isinstance(status, (list, tuple)) else [status]
        self.delay = delay
        self.redirect_to = redirect_to
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                outer.requests.append({"path": self.path, "body": body, "headers": dict(self.headers)})
                if outer.delay:
                    time.sleep(outer.delay)
                try:
                    if outer.redirect_to:
                        self.send_response(302)
                        self.send_header("Location", outer.redirect_to)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    code = outer._statuses.pop(0) if len(outer._statuses) > 1 else outer._statuses[0]
                    payload = b"secret-response-body-do-not-log"
                    self.send_response(code)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):  # silence
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook/SECRET-PATH-TOKEN"

    def start(self) -> "WebhookReceiver":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def make_certificate(directory) -> tuple[str, str]:
    """A self-signed certificate for 127.0.0.1 and localhost: (cert_path, key_path)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = str(directory / "smtp-cert.pem")
    key_path = str(directory / "smtp-key.pem")
    with open(cert_path, "wb") as handle:
        handle.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as handle:
        handle.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    return cert_path, key_path


class SmtpSink:
    """Takes messages. `tls`: None (plain), "starttls" (offered, and auth refused before it) or "ssl"
    (implicit). `require_auth` demands AUTH PLAIN. `temp_fail_first` answers the first N MAIL
    commands with 451 (a retryable failure)."""

    def __init__(self, *, tls=None, cert=None, require_auth=False, temp_fail_first=0) -> None:
        self.messages: list[dict] = []
        self.connections = 0
        self.tls_mode = tls
        self.require_auth = require_auth
        self.temp_fail_first = temp_fail_first
        self._context = None
        if tls:
            self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._context.load_cert_chain(cert[0], cert[1])
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):  # noqa: D401
                try:
                    outer._session(self.request)
                except (OSError, ssl.SSLError):
                    pass

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "SmtpSink":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _session(self, sock: socket.socket) -> None:
        self.connections += 1
        sock.settimeout(10)
        secure = False
        if self.tls_mode == "ssl":
            sock = self._context.wrap_socket(sock, server_side=True)
            secure = True
        reader = sock.makefile("rb")

        def send(line: str) -> None:
            sock.sendall(line.encode() + b"\r\n")

        send("220 localhost ESMTP test sink")
        mail_from, rcpts, auth = None, [], None
        while True:
            raw = reader.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            command = line.split(" ", 1)[0].upper()
            if command == "EHLO" or command == "HELO":
                lines = ["250-localhost"]
                if self.tls_mode == "starttls" and not secure:
                    lines.append("250-STARTTLS")
                if self.require_auth:
                    lines.append("250-AUTH PLAIN")
                lines.append("250 8BITMIME")
                for item in lines:
                    send(item)
            elif command == "STARTTLS" and self.tls_mode == "starttls" and not secure:
                send("220 ready")
                sock = self._context.wrap_socket(sock, server_side=True)
                reader = sock.makefile("rb")
                secure = True
            elif command == "AUTH":
                if self.tls_mode and not secure:
                    send("530 Must issue STARTTLS first")
                    continue
                parts = line.split(" ")
                decoded = base64.b64decode(parts[2]).split(b"\x00") if len(parts) > 2 else []
                auth = (decoded[1].decode(), decoded[2].decode()) if len(decoded) == 3 else None
                send("235 ok")
            elif command == "MAIL":
                if self.require_auth and auth is None:
                    send("530 auth required")
                    continue
                if self.temp_fail_first > 0:
                    self.temp_fail_first -= 1
                    send("451 try again later")
                    continue
                mail_from, rcpts = line.split(":", 1)[1].strip(), []
                send("250 ok")
            elif command == "RCPT":
                rcpts.append(line.split(":", 1)[1].strip())
                send("250 ok")
            elif command == "DATA":
                send("354 end with .")
                chunks = []
                while True:
                    row = reader.readline()
                    if row in (b".\r\n", b""):
                        break
                    chunks.append(row)
                self.messages.append(
                    {
                        "mail_from": mail_from,
                        "rcpts": rcpts,
                        "data": b"".join(chunks).decode("utf-8", "replace"),
                        "secure": secure,
                        "auth": auth,
                    }
                )
                send("250 queued")
            elif command == "RSET" or command == "NOOP":
                send("250 ok")
            elif command == "QUIT":
                send("221 bye")
                return
            else:
                send("502 not implemented")
