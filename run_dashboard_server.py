from __future__ import annotations

import http.server
import socketserver
from pathlib import Path


PORT = 8765
DIRECTORY = Path(__file__).resolve().parent / "dashboard"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIRECTORY), **kwargs)


with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
    print(f"Dashboard running at http://127.0.0.1:{PORT}")
    httpd.serve_forever()
