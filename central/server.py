#!/usr/bin/env python3
"""Minimal central API for VPS traffic monitor.

Provides:
- ingest endpoint with HMAC verification
- node config endpoint: monthly quota, reset day, login verification
- one-click install/uninstall script generation for agent nodes
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, asdict
from typing import Dict

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, HttpUrl, conint, field_validator

app = FastAPI(title="VPS Traffic Monitor Central API", version="0.2.0")


@dataclass
class NodeConfig:
    node_id: str
    monthly_quota_gb: int = 1024
    reset_day: int = 1
    login_verify_enabled: bool = True
    install_script_url: str | None = None
    uninstall_script_url: str | None = None
    agent_endpoint: str = "https://central.example.com/api/v1/ingest"
    agent_api_key: str = "demo-key"
    agent_hmac_secret: str = "demo-secret"
    agent_iface: str = "eth0"
    agent_interval: int = 120


# demo in-memory stores (MVP)
NODE_CONFIGS: Dict[str, NodeConfig] = {}
NODE_SECRETS: Dict[str, dict] = {
    "demo-key": {"hmac_secret": "demo-secret", "node_id": "demo-node"}
}
INGEST_CACHE = set()


class ConfigUpdate(BaseModel):
    monthly_quota_gb: conint(ge=1, le=1024 * 1024) = Field(..., description="Monthly traffic quota in GB")
    reset_day: conint(ge=1, le=31)
    login_verify_enabled: bool
    install_script_url: HttpUrl | None = None
    uninstall_script_url: HttpUrl | None = None
    agent_endpoint: HttpUrl
    agent_api_key: str = Field(..., min_length=3)
    agent_hmac_secret: str = Field(..., min_length=6)
    agent_iface: str = Field(default="eth0", min_length=1)
    agent_interval: conint(ge=30, le=3600) = 120

    @field_validator("install_script_url", "uninstall_script_url", "agent_endpoint")
    @classmethod
    def enforce_https(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is None:
            return value
        if value.scheme != "https":
            raise ValueError("url must use https")
        return value


class IngestPayload(BaseModel):
    node_id: str
    timestamp: str
    nonce: str
    iface: str
    counters: dict
    hourly: list
    daily: list
    hostname: str | None = None
    agent_version: str | None = None


def verify_sig(secret: str, timestamp: str, nonce: str, body: bytes, signature: str) -> bool:
    msg = f"{timestamp}.{nonce}.".encode() + body
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def build_one_click_script(cfg: NodeConfig, action: str) -> str:
    if action not in {"install", "uninstall"}:
        raise ValueError("action must be install/uninstall")

    if action == "install":
        return f"""#!/usr/bin/env bash
set -euo pipefail

ACTION="${{1:-install}}"
if [[ "$ACTION" != "install" ]]; then
  echo "unsupported action: $ACTION" >&2
  exit 1
fi

install -d /opt/vps-traffic-monitor /etc/vps-traffic-monitor /var/log/vps-traffic-monitor

if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y curl python3 vnstat
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y curl python3 vnstat
elif command -v yum >/dev/null 2>&1; then
  yum install -y curl python3 vnstat
else
  echo "unsupported package manager" >&2
  exit 1
fi

systemctl enable --now vnstat || true
curl -fsSL {cfg.install_script_url or 'https://example.com/traffic_agent.py'} -o /opt/vps-traffic-monitor/traffic_agent.py
chmod +x /opt/vps-traffic-monitor/traffic_agent.py

cat >/etc/vps-traffic-monitor/agent.env <<'EOF'
ENDPOINT={cfg.agent_endpoint}
API_KEY={cfg.agent_api_key}
HMAC_SECRET={cfg.agent_hmac_secret}
NODE_ID={cfg.node_id}
IFACE={cfg.agent_iface}
INTERVAL={cfg.agent_interval}
EOF

cat >/etc/systemd/system/vps-traffic-agent.service <<'EOF'
[Unit]
Description=VPS Traffic Monitor Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/vps-traffic-monitor/agent.env
ExecStart=/usr/bin/python3 /opt/vps-traffic-monitor/traffic_agent.py \\
  --endpoint $ENDPOINT \\
  --api-key $API_KEY \\
  --hmac-secret $HMAC_SECRET \\
  --node-id $NODE_ID \\
  --iface $IFACE \\
  --interval $INTERVAL
Restart=always
RestartSec=10
StandardOutput=append:/var/log/vps-traffic-monitor/agent.log
StandardError=append:/var/log/vps-traffic-monitor/agent.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now vps-traffic-agent.service
echo "install done"
"""

    return """#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-uninstall}"
if [[ "$ACTION" != "uninstall" ]]; then
  echo "unsupported action: $ACTION" >&2
  exit 1
fi

systemctl disable --now vps-traffic-agent.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/vps-traffic-agent.service
systemctl daemon-reload
rm -rf /opt/vps-traffic-monitor /etc/vps-traffic-monitor
rm -f /var/log/vps-traffic-monitor/agent.log

echo "uninstall done"
"""


@app.get("/", response_class=HTMLResponse)
def home_page():
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>VPS 流量监控</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; margin: 2rem; background: #f8fafc; color: #0f172a; }
    .card { max-width: 840px; background: #fff; border-radius: 12px; padding: 1.2rem; box-shadow: 0 2px 12px rgba(0,0,0,.08); }
    input, button { padding: .55rem .7rem; font-size: 15px; }
    button { cursor: pointer; border: 0; background: #2563eb; color: #fff; border-radius: 8px; }
    code { background: #f1f5f9; padding: .1rem .3rem; border-radius: 4px; }
    pre { overflow: auto; background: #0b1020; color: #dbeafe; padding: 1rem; border-radius: 8px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>VPS 流量监控中心</h1>
    <p>输入节点 ID 后可直接查询配置接口，便于快速检查部署状态。</p>
    <p>API 文档：<a href="/docs" target="_blank">/docs</a></p>

    <label for="nodeId">节点 ID：</label>
    <input id="nodeId" value="demo-node" />
    <button onclick="loadConfig()">查询配置</button>

    <p style="margin-top:1rem">接口：<code id="url">/api/v1/nodes/demo-node/config</code></p>
    <pre id="output">点击“查询配置”后显示结果...</pre>
  </div>

  <script>
    async function loadConfig(){
      const nodeId = document.getElementById('nodeId').value.trim() || 'demo-node';
      const url = `/api/v1/nodes/${encodeURIComponent(nodeId)}/config`;
      document.getElementById('url').textContent = url;
      try {
        const res = await fetch(url);
        const data = await res.json();
        document.getElementById('output').textContent = JSON.stringify(data, null, 2);
      } catch (e) {
        document.getElementById('output').textContent = `请求失败: ${e}`;
      }
    }
  </script>
</body>
</html>"""

@app.get("/api/v1/nodes/{node_id}/config")
def get_node_config(node_id: str):
    cfg = NODE_CONFIGS.get(node_id) or NodeConfig(node_id=node_id)
    NODE_CONFIGS[node_id] = cfg
    return asdict(cfg)


@app.put("/api/v1/nodes/{node_id}/config")
def update_node_config(node_id: str, update: ConfigUpdate):
    cfg = NodeConfig(node_id=node_id, **update.model_dump())
    NODE_CONFIGS[node_id] = cfg
    return {"ok": True, "config": asdict(cfg)}


@app.get("/api/v1/nodes/{node_id}/scripts/{action}.sh")
def get_node_script(node_id: str, action: str):
    cfg = NODE_CONFIGS.get(node_id) or NodeConfig(node_id=node_id)
    NODE_CONFIGS[node_id] = cfg
    script = build_one_click_script(cfg, action)
    return Response(content=script, media_type="text/x-shellscript")


@app.post("/api/v1/ingest")
def ingest(
    payload: IngestPayload,
    x_api_key: str = Header(...),
    x_timestamp: str = Header(...),
    x_nonce: str = Header(...),
    x_signature: str = Header(...),
):
    key_cfg = NODE_SECRETS.get(x_api_key)
    if not key_cfg:
        raise HTTPException(status_code=401, detail="invalid api key")

    if key_cfg["node_id"] != payload.node_id:
        raise HTTPException(status_code=403, detail="api key not allowed for node")

    now = int(time.time())
    ts = int(time.mktime(time.strptime(x_timestamp.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")))
    if abs(now - ts) > 300:
        raise HTTPException(status_code=401, detail="timestamp outside 5-minute window")

    dedupe_key = f"{payload.node_id}:{x_timestamp}:{x_nonce}"
    if dedupe_key in INGEST_CACHE:
        return {"ok": True, "deduped": True}

    body = json.dumps(payload.model_dump(), separators=(",", ":")).encode()
    if not verify_sig(key_cfg["hmac_secret"], x_timestamp, x_nonce, body, x_signature):
        raise HTTPException(status_code=401, detail="bad signature")

    INGEST_CACHE.add(dedupe_key)
    return {"ok": True, "stored": True}
