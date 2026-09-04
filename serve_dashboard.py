"""Serve the real terminal governance dashboard (dashboard/app.py) over HTTP.

Uses textual-serve to expose the actual Textual TUI in a browser tab
(terminal-in-a-page over a websocket), not a simplified HTML mockup.
"""

from __future__ import annotations

import os

from textual_serve.server import Server

port = int(os.environ.get("PORT", 8000))
# Render sets RENDER_EXTERNAL_URL to the service's real public HTTPS URL.
# Without it, textual-serve bakes the bind host (0.0.0.0) into every asset
# URL in the generated page, which is unreachable from a browser.
public_url = os.environ.get("RENDER_EXTERNAL_URL")
server = Server(
    "python dashboard/app.py --audit-file audit.jsonl --policy-file policies/default.yaml",
    host="0.0.0.0",
    port=port,
    title="Backstop | Trade Firewall Monitor",
    public_url=public_url,
)
server.serve()
