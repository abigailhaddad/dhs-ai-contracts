"""Shared helpers for browser-driven frontend tests.

Mirrors the pattern in pull_usaspending/tests/conftest.py: each browser
test owns its own port + fixture_server fixture so they parallelize
under pytest-xdist without colliding. Helpers live here to avoid
duplicating the boilerplate.
"""

from __future__ import annotations

import os
import socket
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_quiet_server(serve_dir: Path, port: int) -> HTTPServer:
    """Start an HTTPServer rooted at `serve_dir` on `port` in a daemon
    thread. Caller is responsible for `server.shutdown()`."""
    os.chdir(serve_dir)

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
