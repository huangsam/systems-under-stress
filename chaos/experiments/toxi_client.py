"""Lightweight Toxiproxy helper using standard library urllib."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

TOXIPROXY_URL = "http://127.0.0.1:8474"
PROXY_NAME = "redis"
LISTEN_ADDR = "0.0.0.0:26379"
UPSTREAM_ADDR = "redis-upstream:6379"


def _request(endpoint: str, method: str = "GET", data: dict[str, Any] | None = None) -> Any:
    url = f"{TOXIPROXY_URL}{endpoint}"
    payload = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as exc:
        err_msg = exc.read().decode("utf-8")
        try:
            return json.loads(err_msg)
        except Exception:
            raise RuntimeError(f"Toxiproxy HTTP {exc.code}: {err_msg}") from exc


def ensure_proxy() -> dict[str, Any]:
    """Ensures the redis proxy is created and enabled."""
    proxies = _request("/proxies", method="GET")
    if PROXY_NAME in proxies:
        return proxies[PROXY_NAME]

    return _request(
        "/proxies",
        method="POST",
        data={
            "name": PROXY_NAME,
            "listen": LISTEN_ADDR,
            "upstream": UPSTREAM_ADDR,
            "enabled": True,
        },
    )


def reset_all() -> None:
    """Wipes all active toxics and re-enables all proxies."""
    _request("/reset", method="POST", data={})


def list_toxics() -> list[dict[str, Any]]:
    """Lists currently active toxics on the redis proxy."""
    resp = _request(f"/proxies/{PROXY_NAME}/toxics", method="GET")
    return resp if isinstance(resp, list) else []


def add_latency(
    name: str = "latency_toxic",
    latency_ms: int = 200,
    jitter_ms: int = 0,
    stream: str = "downstream",
) -> dict[str, Any]:
    return _request(
        f"/proxies/{PROXY_NAME}/toxics",
        method="POST",
        data={
            "name": name,
            "type": "latency",
            "stream": stream,
            "attributes": {"latency": latency_ms, "jitter": jitter_ms},
        },
    )


def add_bandwidth(
    name: str = "bandwidth_toxic",
    rate_kb: int = 20,
    stream: str = "downstream",
) -> dict[str, Any]:
    return _request(
        f"/proxies/{PROXY_NAME}/toxics",
        method="POST",
        data={
            "name": name,
            "type": "bandwidth",
            "stream": stream,
            "attributes": {"rate": rate_kb},
        },
    )


def add_timeout(
    name: str = "timeout_toxic",
    timeout_ms: int = 0,
    stream: str = "downstream",
) -> dict[str, Any]:
    return _request(
        f"/proxies/{PROXY_NAME}/toxics",
        method="POST",
        data={
            "name": name,
            "type": "timeout",
            "stream": stream,
            "attributes": {"timeout": timeout_ms},
        },
    )


def add_reset_peer(
    name: str = "reset_peer_toxic",
    stream: str = "downstream",
) -> dict[str, Any]:
    return _request(
        f"/proxies/{PROXY_NAME}/toxics",
        method="POST",
        data={
            "name": name,
            "type": "reset_peer",
            "stream": stream,
            "attributes": {},
        },
    )


def remove_toxic(name: str) -> None:
    try:
        _request(f"/proxies/{PROXY_NAME}/toxics/{name}", method="DELETE")
    except Exception:
        pass
