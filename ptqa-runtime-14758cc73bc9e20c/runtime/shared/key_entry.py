#!/usr/bin/env python3
"""One-use, local Tiingo key entry. No key arguments, browser launch or probe.

Run this on the member's computer, then open the printed URL on that computer.
The sibling ptq_data module owns storage (PTQ_ACADEMY_HOME is read on import).
Browser autocomplete preferences are advisory; this is not an OS keychain.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import math
from pathlib import Path
import re
import secrets
import socket
from socketserver import TCPServer
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

MAX_BODY = 4096
MAX_KEY = 512
TITLE = "Save your Tiingo key"
KEY_LABEL = "Tiingo API key"
SAVE_LABEL = "Save key locally"
SAVE_NOTE = "Stores the key on this computer. Does not verify the connection."
SUCCESS = "Key saved locally. Run the separate connection probe next. You can close this tab."


def _storage_writer():
    # Load this exact sibling, independent of cwd, sys.path and cached modules.
    spec = importlib.util.spec_from_file_location(
        "_ptq_key_entry_data", Path(__file__).with_name("ptq_data.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.set_key


def _page(content):
    return ("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{TITLE}</title><style>"
            "*{box-sizing:border-box;letter-spacing:0}"
            "body{margin:0;background:#0f0f0f;color:#f6f5f2;font:16px/1.5 system-ui,sans-serif}"
            "main{max-width:560px;margin:48px auto;padding:24px;overflow-wrap:anywhere}"
            "h1{font-size:26px;line-height:1.2}p{color:#aba9a4}"
            "label{display:block;margin:24px 0 8px;font-family:monospace}"
            "input,button{width:100%;min-height:48px;padding:12px;border-radius:4px;font:inherit}"
            "input{background:#2a2929;color:#f6f5f2;border:1px solid #aba9a4}"
            "button{margin-top:24px;background:#ffd000;color:#0f0f0f;border:0;cursor:pointer}"
            "button:focus-visible,input:focus-visible{outline:3px solid #5aa2ff;outline-offset:3px}"
            "</style><main>" + content + "</main></html>").encode("utf-8")


class KeyEntryServer(HTTPServer):
    """Serial writes; serve() owns expiry and closes all listening resources."""

    allow_reuse_address = False

    def __init__(self, *, timeout=300, write_key=None):
        if not math.isfinite(timeout) or not 0 < timeout <= 1800:
            raise ValueError("Timeout must be between 0 and 1800 seconds.")
        self.write_key = write_key if write_key is not None else _storage_writer()
        self.session_path = "/" + secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.saved = False
        self.expired = threading.Event()
        self._active_lock = threading.Lock()
        self._active = None
        self.deadline = time.monotonic() + timeout
        super().__init__(("127.0.0.1", 0), _Handler)
        self.timeout = 0.1
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.host = f"127.0.0.1:{self.server_port}"
        self.url = self.origin + self.session_path

    def server_bind(self):
        # HTTPServer normally calls getfqdn(); no DNS is needed for loopback.
        TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]

    def get_request(self):
        connection, address = super().get_request()
        with self._active_lock:
            self._active = connection
            connection.settimeout(max(0.001, min(5, self.deadline - time.monotonic())))
            if self.expired.is_set():
                connection.close()
                raise OSError("Session expired")
        return connection, address

    def close_request(self, request):
        with self._active_lock:
            if self._active is request:
                self._active = None
            super().close_request(request)

    def handle_error(self, request, client_address):
        # The default implementation prints a traceback, potentially with secrets.
        pass

    def _expire(self):
        self.expired.set()
        with self._active_lock:
            if self._active is not None:
                try:
                    self._active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def serve(self):
        """Return True after a save, False on expiry; never return the key."""
        timer = threading.Timer(max(0, self.deadline - time.monotonic()), self._expire)
        timer.daemon = True
        timer.start()
        try:
            while not self.saved and not self.expired.is_set():
                self.handle_request()
            return self.saved
        finally:
            timer.cancel()
            self.server_close()


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 deliberately closes each connection, including malformed requests.
    server_version = "LocalKeyEntry"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self._reply(code, "Request rejected.")

    def _reply(self, status, text, *, form=False):
        body = _page(text if form else f"<p>{html.escape(text)}</p>")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Native form POSTs can send Origin: null under no-referrer. Preserve
        # the origin for CSRF validation without including the session path.
        self.send_header("Referrer-Policy", "strict-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                         "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def _one_header(self, name, expected):
        return self.headers.get_all(name, []) == [expected]

    def _session_ok(self):
        if not self._one_header("Host", self.server.host):
            self._reply(403, "Request rejected.")
        elif self.path != self.server.session_path:
            self._reply(404, "Page not found.")
        elif (self.server.saved or self.server.expired.is_set()
              or time.monotonic() >= self.server.deadline):
            self._reply(410, "This entry session has ended.")
        else:
            return True
        return False

    def do_GET(self):
        if not self._session_ok():
            return
        self._reply(200, f"<h1>{TITLE}</h1>"
                    f'<form method="post" action="{self.server.session_path}" autocomplete="off">'
                    '<input type="hidden" name="provider" value="tiingo">'
                    f'<input type="hidden" name="csrf" value="{self.server.csrf}">'
                    f'<label for="token">{KEY_LABEL}</label>'
                    f'<input id="token" name="token" type="password" autocomplete="off" '
                    f'spellcheck="false" autocapitalize="none" maxlength="{MAX_KEY}" required>'
                    f'<button type="submit" aria-describedby="save-note">{SAVE_LABEL}</button>'
                    f'<p id="save-note">{SAVE_NOTE}</p></form>', form=True)

    def do_POST(self):
        if not self._session_ok():
            return
        if (not self._one_header("Origin", self.server.origin)
                or self.headers.get_all("Sec-Fetch-Site", []) not in ([], ["same-origin"])):
            self._reply(403, "Request rejected.")
            return
        if (not self._one_header("Content-Type", "application/x-www-form-urlencoded")
                or self.headers.get_all("Transfer-Encoding")
                or self.headers.get_all("Expect")):
            self._reply(400, "Request rejected.")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,5}", lengths[0]):
            self._reply(400, "Request rejected.")
            return
        length = int(lengths[0])
        if not 0 < length <= MAX_BODY:
            self._reply(413, "Request too large or empty.")
            return
        raw = self.rfile.read(length)
        try:
            if len(raw) != length or re.search(rb"%(?![0-9a-fA-F]{2})", raw):
                raise ValueError
            fields = parse_qs(raw.decode("ascii"), keep_blank_values=True,
                              strict_parsing=True, encoding="utf-8", errors="strict",
                              max_num_fields=3)
            if set(fields) != {"provider", "csrf", "token"} or any(len(v) != 1 for v in fields.values()):
                raise ValueError
            if fields["provider"] != ["tiingo"]:
                raise ValueError
            if not secrets.compare_digest(fields["csrf"][0].encode(), self.server.csrf.encode()):
                self._reply(403, "Request rejected.")
                return
            token = fields["token"][0].strip()
            if not 0 < len(token) <= MAX_KEY or any(not 33 <= ord(c) <= 126 for c in token):
                raise ValueError
        except (ValueError, UnicodeError):
            self._reply(400, "Enter a Tiingo API key.")
            return
        if self.server.expired.is_set() or time.monotonic() >= self.server.deadline:
            self._reply(410, "This entry session has ended.")
            return
        try:
            self.server.write_key("tiingo", token)
        except Exception:
            self._reply(500, "Could not save the key locally. Try again before this session expires.")
            return
        # Consume before sending: even a dropped response cannot cause another write.
        self.server.saved = True
        self._reply(200, SUCCESS)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Never echo unexpected arguments: they could be an accidentally pasted key.
        self.exit(2, "Invalid arguments. Use --help. Never pass a key as an argument.\n")


def main(argv=None):
    parser = _Parser(description="Enter a Tiingo key in a masked local browser form. "
                     "Open the printed URL on this computer. Saving does not verify the connection.")
    parser.add_argument("--timeout", type=int, default=300,
                        help="session lifetime in seconds, 1-1800 (default: 300)")
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 1800:
        parser.error("timeout")
    try:
        with KeyEntryServer(timeout=args.timeout) as server:
            print(server.url, flush=True)
            saved = server.serve()
        print("Key saved locally. Connection not verified." if saved else
              "Entry session timed out. No key saved.", file=sys.stderr)
        return 0 if saved else 1
    except KeyboardInterrupt:
        print("Entry session stopped.", file=sys.stderr)
        return 130
    except Exception:
        print("Local key entry could not complete.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
