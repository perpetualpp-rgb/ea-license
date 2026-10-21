"""Tiny stdlib HTTP helper so the lab needs no third-party packages."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

USER_AGENT = "orderflow-lab/0.1 (+https://github.com/perpetualpp-rgb/ea-license)"


def get_json(url: str, params: dict | None = None, timeout: float = 10.0):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
