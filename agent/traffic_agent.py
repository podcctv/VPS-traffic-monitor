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
from urllib.parse import urlparse
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


def get_node_config(config_url: str, timeout: int = 10) -> dict:
    req = request.Request(url=config_url, method="GET")
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def get_next_action(action_url: str, api_key: str, timeout: int = 10) -> dict:
    req = request.Request(url=f"{action_url}?api_key={api_key}", method="GET")
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def execute_action(action: str) -> None:
    if action != "uninstall":
        return
    subprocess.run(["bash", "/usr/local/bin/vtm-agent", "uninstall"], check=True)


def run_one_click_from_config(config: dict, action: str) -> None:
    field = "install_script_url" if action == "install" else "uninstall_script_url"
    script_url = config.get(field)
    if not script_url:
        raise RuntimeError(f"central config missing {field}")
    parsed = urlparse(script_url)
    if parsed.scheme != "https":
        raise RuntimeError("one-click script URL must use HTTPS")
    cmd = f"curl -fsSL {script_url} | bash -s -- {action}"
    subprocess.run(["bash", "-lc", cmd], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="vnStat agent uploader")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--hmac-secret", required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--iface")
    parser.add_argument("--interval", type=int, default=0, help="seconds, 0=run once")
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument(
        "--one-click",
        choices=["install", "uninstall"],
        help="fetch node config from central and execute install/uninstall script",
    )
    parser.add_argument(
        "--config-endpoint-template",
        default="{base}/api/v1/nodes/{node_id}/config",
        help="template used to build config endpoint for --one-click",
    )
    parser.add_argument(
        "--action-endpoint-template",
        default="{base}/api/v1/nodes/{node_id}/actions/next",
        help="template used to query central for remote actions",
    )
    args = parser.parse_args()

    if args.one_click:
        endpoint_base = args.endpoint.rsplit("/api/v1/ingest", 1)[0]
        config_url = args.config_endpoint_template.format(base=endpoint_base, node_id=args.node_id)
        config = get_node_config(config_url)
        run_one_click_from_config(config, args.one_click)
        print(f"[{iso_now()}] one-click action={args.one_click} done")
        return 0

    while True:
        data = run_vnstat_json()
        iface_data = pick_interface(data, args.iface)
        payload = build_payload(args.node_id, iface_data, args.version)
        status, body = post_payload(args.endpoint, args.api_key, args.hmac_secret, payload)
        print(f"[{iso_now()}] upload status={status} body={body}")
        endpoint_base = args.endpoint.rsplit("/api/v1/ingest", 1)[0]
        action_url = args.action_endpoint_template.format(base=endpoint_base, node_id=args.node_id)
        action_resp = get_next_action(action_url, args.api_key)
        if action_resp.get("action"):
            execute_action(action_resp["action"])
            print(f"[{iso_now()}] remote action executed: {action_resp['action']}")
            break
        if args.interval <= 0:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
