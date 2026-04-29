#!/usr/bin/env python3
"""vnStat traffic agent.

Features:
- reads vnstat --json output
- normalizes counters/hourly/daily for one interface
- signs payload using HMAC-SHA256
- posts payload to central ingest API
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import secrets
import socket
import subprocess
import time
from datetime import datetime, timezone
from urllib import request


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_vnstat_json() -> dict:
    proc = subprocess.run(["vnstat", "--json"], check=True, text=True, capture_output=True)
    return json.loads(proc.stdout)


def pick_interface(data: dict, iface: str | None) -> dict:
    interfaces = data.get("interfaces", [])
    if not interfaces:
        raise RuntimeError("vnstat JSON missing interfaces")
    if iface:
        for item in interfaces:
            if item.get("name") == iface:
                return item
        raise RuntimeError(f"interface not found: {iface}")
    return interfaces[0]


def build_payload(node_id: str, iface_data: dict, version: str) -> dict:
    traffic = iface_data.get("traffic", {})
    total = traffic.get("total", {})
    days = traffic.get("day", [])
    months = traffic.get("month", [])
    hours = traffic.get("hour", [])

    today = days[-1] if days else {"rx": 0, "tx": 0}
    month = months[-1] if months else {"rx": 0, "tx": 0}

    hourly = []
    for h in hours[-24:]:
        ts = datetime(h["date"]["year"], h["date"]["month"], h["date"]["day"], h.get("time", {}).get("hour", 0), tzinfo=timezone.utc)
        hourly.append({"time": ts.isoformat().replace("+00:00", "Z"), "rx": int(h.get("rx", 0)), "tx": int(h.get("tx", 0))})

    daily = []
    for d in days[-30:]:
        daily.append({
            "date": f"{d['date']['year']:04d}-{d['date']['month']:02d}-{d['date']['day']:02d}",
            "rx": int(d.get("rx", 0)),
            "tx": int(d.get("tx", 0)),
        })

    return {
        "node_id": node_id,
        "hostname": socket.gethostname(),
        "timestamp": iso_now(),
        "iface": iface_data.get("name", "unknown"),
        "counters": {
            "rx_total_bytes": int(total.get("rx", 0)),
            "tx_total_bytes": int(total.get("tx", 0)),
            "rx_today_bytes": int(today.get("rx", 0)),
            "tx_today_bytes": int(today.get("tx", 0)),
            "rx_month_bytes": int(month.get("rx", 0)),
            "tx_month_bytes": int(month.get("tx", 0)),
        },
        "hourly": hourly,
        "daily": daily,
        "agent_version": version,
        "nonce": secrets.token_hex(16),
    }


def sign_payload(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    msg = f"{timestamp}.{nonce}.".encode() + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def post_payload(url: str, api_key: str, secret: str, payload: dict, timeout: int = 10) -> tuple[int, str]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    ts = payload["timestamp"]
    nonce = payload["nonce"]
    sig = sign_payload(secret, ts, nonce, body)

    req = request.Request(url=url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-Key", api_key)
    req.add_header("X-Timestamp", ts)
    req.add_header("X-Nonce", nonce)
    req.add_header("X-Signature", sig)

    with request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode()


def main() -> int:
    parser = argparse.ArgumentParser(description="vnStat agent uploader")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--hmac-secret", required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--iface")
    parser.add_argument("--interval", type=int, default=0, help="seconds, 0=run once")
    parser.add_argument("--version", default="1.0.0")
    args = parser.parse_args()

    while True:
        data = run_vnstat_json()
        iface_data = pick_interface(data, args.iface)
        payload = build_payload(args.node_id, iface_data, args.version)
        status, body = post_payload(args.endpoint, args.api_key, args.hmac_secret, payload)
        print(f"[{iso_now()}] upload status={status} body={body}")
        if args.interval <= 0:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
