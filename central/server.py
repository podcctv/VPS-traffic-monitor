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
import os
import secrets
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from dataclasses import dataclass, asdict
from typing import Dict

from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, HttpUrl, conint, field_validator

app = FastAPI(title="VPS Traffic Monitor Central API", version="0.3.0")
BASE_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = BASE_DIR / "scripts"


@dataclass
class NodeConfig:
    node_id: str
    monthly_quota_gb: int = 1024
    reset_day: int = 1
    login_verify_enabled: bool = True
    login_verify_token: str = "demo-login-token"
    install_script_url: str | None = None
    uninstall_script_url: str | None = None
    agent_endpoint: str = "https://central.example.com/api/v1/ingest"
    agent_api_key: str = "demo-key"
    agent_hmac_secret: str = "demo-secret"
    agent_iface: str = "eth0"
    agent_interval: int = 120


NODE_CONFIGS: Dict[str, NodeConfig] = {}
NODE_SECRETS: Dict[str, dict] = {"demo-key": {"hmac_secret": "demo-secret", "node_id": "demo-node"}}
INGEST_CACHE = set()
LATEST_INGEST: Dict[str, dict] = {}
NODE_PENDING_ACTIONS: Dict[str, str] = {}
ADMIN_STATE = {"username": None, "password_hash": None}
ADMIN_SESSIONS = set()


class ConfigUpdate(BaseModel):
    monthly_quota_gb: conint(ge=1, le=1024 * 1024) = Field(..., description="Monthly traffic quota in GB")
    reset_day: conint(ge=1, le=31)
    login_verify_enabled: bool
    login_verify_token: str = Field(..., min_length=6)
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


class QuickSetupRequest(BaseModel):
    node_id: str = Field(..., min_length=1)
    monthly_quota_gb: conint(ge=1, le=1024 * 1024)
    reset_day: conint(ge=1, le=31)
    public_base_url: HttpUrl | None = None
    agent_endpoint: HttpUrl | None = None


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


class LoginVerifyRequest(BaseModel):
    token: str = Field(..., min_length=1)


class AdminCreds(BaseModel):
    username: str = Field(..., min_length=3)
    password: str = Field(..., min_length=6)


def verify_sig(secret: str, timestamp: str, nonce: str, body: bytes, signature: str) -> bool:
    msg = f"{timestamp}.{nonce}.".encode() + body
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def delete_node(node_id: str) -> bool:
    cfg = NODE_CONFIGS.pop(node_id, None)
    if not cfg:
        return False
    NODE_PENDING_ACTIONS.pop(node_id, None)
    LATEST_INGEST.pop(node_id, None)
    INGEST_CACHE.difference_update({k for k in INGEST_CACHE if k.startswith(f"{node_id}|")})
    api_key = cfg.agent_api_key
    NODE_SECRETS.pop(api_key, None)
    return True


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




def build_central_upgrade_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-upgrade}"
SELF_PATH="${SELF_PATH:-/usr/local/bin/vtm-central-upgrade}"
REPO_URL="${REPO_URL:-https://github.com/podcctv/VPS-traffic-monitor.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/VPS-traffic-monitor}"
BRANCH="${BRANCH:-main}"
CENTRAL_URL="${CENTRAL_URL:-http://127.0.0.1:8000}"
SCRIPT_URL="${SCRIPT_URL:-${CENTRAL_URL%/}/api/v1/central/scripts/upgrade.sh}"

if [[ "$ACTION" != "upgrade" ]]; then
  echo "unsupported action: $ACTION" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not installed" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose plugin is required" >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y
    apt-get install -y git curl
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y git curl
  elif command -v yum >/dev/null 2>&1; then
    yum install -y git curl
  else
    echo "git not found and unsupported package manager" >&2
    exit 1
  fi
fi

if [[ -d "$INSTALL_DIR/.git" ]]; then
  git -C "$INSTALL_DIR" fetch --all --prune
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
else
  rm -rf "$INSTALL_DIR"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
docker compose pull || true
docker compose up -d --build --remove-orphans

# Optional: refresh local upgrade script file itself.
if [[ -w "$(dirname "$SELF_PATH")" ]] && command -v curl >/dev/null 2>&1; then
  tmp="$(mktemp)"
  if curl -fsSL "$SCRIPT_URL" -o "$tmp"; then
    install -m 0755 "$tmp" "$SELF_PATH"
    echo "upgrade script refreshed: $SELF_PATH"
  fi
  rm -f "$tmp"
fi

echo "central upgrade done"
"""

def _external_base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _require_admin(session: str | None):
    if not ADMIN_STATE["password_hash"]:
        raise HTTPException(status_code=403, detail="admin not initialized")
    if not session or session not in ADMIN_SESSIONS:
        raise HTTPException(status_code=401, detail="unauthorized")


def _script_base_url(request: Request) -> str:
    return os.getenv("SCRIPT_BASE_URL", _external_base_url(request))


@app.get("/", response_class=HTMLResponse)
def public_page():
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>VPS 流量星图</title><script src="https://cdn.tailwindcss.com"></script><script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script></head>
<body class="min-h-screen bg-slate-950 text-slate-100"><div id="app" class="max-w-6xl mx-auto p-6 space-y-6">
<div class="rounded-3xl border border-cyan-400/30 bg-gradient-to-br from-slate-900 via-indigo-950 to-slate-900 shadow-2xl p-6"><div class="flex items-center justify-between gap-4"><div><h1 class="text-3xl font-black tracking-wide">🚀 VPS 流量星图</h1><p class="text-cyan-200 mt-1">公开展示页（无需登录）</p></div><div class="flex gap-2"><a href="/admin" class="px-4 py-2 rounded-lg bg-cyan-500 text-slate-900 font-bold">进入配置后台</a><button @click="loadBoard" class="px-4 py-2 rounded-lg bg-fuchsia-500 font-bold">刷新</button></div></div></div>
<div class="grid md:grid-cols-3 gap-4"><div class="rounded-2xl p-5 bg-slate-900 border border-slate-700"><p class="text-slate-400">在线节点</p><p class="text-4xl font-black text-cyan-300">{{ stats.nodes }}</p></div><div class="rounded-2xl p-5 bg-slate-900 border border-slate-700"><p class="text-slate-400">总用量</p><p class="text-4xl font-black text-emerald-300">{{ stats.total }}</p></div><div class="rounded-2xl p-5 bg-slate-900 border border-slate-700"><p class="text-slate-400">最近更新</p><p class="text-xl font-bold text-fuchsia-300">{{ stats.latest }}</p></div></div>
<div class="grid lg:grid-cols-2 gap-4"><div v-for="row in rows" :key="row.node_id" class="rounded-2xl p-5 bg-slate-900/80 border border-cyan-500/30 shadow-lg"><div class="flex items-center justify-between"><h3 class="text-xl font-bold">{{ row.node_id }}</h3><span class="text-xs px-2 py-1 rounded bg-cyan-400/20 text-cyan-200">{{ row.iface }}</span></div><p class="mt-4 text-3xl font-black text-emerald-300">{{ row.used }}</p><p class="text-slate-400 mt-1">月配额 {{ row.monthly_quota_gb }} GB · 重置日 {{ row.reset_day }}</p><div class="mt-3 h-2 rounded bg-slate-700"><div class="h-2 rounded bg-gradient-to-r from-cyan-400 to-fuchsia-500" :style="{width: row.percent + '%'}"></div></div><p class="text-xs text-slate-500 mt-2">最后上报：{{ row.timestamp }}</p></div><div v-if="rows.length===0" class="rounded-2xl p-6 border border-dashed border-slate-600 text-slate-400">暂无数据，请稍后刷新。</div></div></div>
<script>const {createApp}=Vue;createApp({data(){return{rows:[],stats:{nodes:0,total:'0 GiB',latest:'-'}}},methods:{async loadBoard(){const res=await fetch('/api/v1/public-dashboard');if(!res.ok){this.rows=[];return;}const data=await res.json();let sum=0,latest='-';this.rows=(data.nodes||[]).map(n=>{const li=(data.latest_ingest||{})[n.node_id]||{};const used=Number((li.counters&&li.counters.total_gib)||0);sum+=used;if(li.timestamp&&li.timestamp>latest){latest=li.timestamp;}const percent=Math.min(100,Math.round((used/Math.max(1,n.monthly_quota_gb))*100));return {...n,used:`${used.toFixed(2)} GiB`,iface:li.iface||n.agent_iface||'-',timestamp:li.timestamp||'-',percent};});this.stats={nodes:this.rows.length,total:`${sum.toFixed(2)} GiB`,latest};}},async mounted(){await this.loadBoard();setInterval(this.loadBoard,15000);}}).mount('#app');</script></body></html>"""


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>VPS 流量监控中心（配置后台）</title><script src="https://cdn.tailwindcss.com"></script><script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script></head>
<body class="bg-slate-100 text-slate-800"><div id="app" class="max-w-6xl mx-auto p-6">
<div class="bg-white rounded-2xl shadow-xl p-6 space-y-5"><div class="flex items-center justify-between"><h1 class="text-2xl font-bold">VPS 流量监控中心（配置后台）</h1><div class="flex gap-4"><a href="/" class="text-fuchsia-700">公开展示页</a><a href="/docs" target="_blank" class="text-blue-600">API 文档</a></div></div>
<div class="rounded-lg border px-4 py-3 bg-slate-50">状态：<span class="font-semibold">{{ authSummary }}</span></div>
<div class="grid md:grid-cols-2 gap-4"><label class="space-y-1"><span>节点 ID</span><input v-model="form.node_id" class="w-full border rounded px-3 py-2"></label><label class="space-y-1"><span>月流量配额(GB)</span><input v-model.number="form.monthly_quota_gb" type="number" min="1" class="w-full border rounded px-3 py-2"></label></div>
<div class="grid md:grid-cols-3 gap-4"><label class="space-y-1"><span>重置日(1-31)</span><input v-model.number="form.reset_day" type="number" min="1" max="31" class="w-full border rounded px-3 py-2"></label><label class="space-y-1 md:col-span-2"><span>公网地址（可选）</span><input v-model="form.public_base_url" placeholder="https://monitor.example.com" class="w-full border rounded px-3 py-2"></label></div>
<label class="space-y-1 block"><span>节点上报地址（可选）</span><input v-model="form.agent_endpoint" placeholder="https://monitor.example.com/api/v1/ingest" class="w-full border rounded px-3 py-2"></label>
<div class="flex flex-wrap gap-3"><button @click="quickSetup" class="bg-blue-600 text-white px-4 py-2 rounded">生成安装命令</button><button @click="genCentralUpgrade" class="bg-emerald-700 text-white px-4 py-2 rounded">生成中心端升级命令</button><button @click="loadDashboard" class="bg-slate-700 text-white px-4 py-2 rounded">刷新节点展示</button></div>
<pre class="bg-slate-900 text-slate-100 p-3 rounded overflow-auto">安装命令：\n{{ installCmd }}</pre><pre class="bg-slate-900 text-slate-100 p-3 rounded overflow-auto">中心端升级命令：\n{{ centralUpgradeCmd }}</pre><pre class="bg-slate-900 text-slate-100 p-3 rounded overflow-auto">当前配置：\n{{ outputText }}</pre>
<div class="overflow-auto"><table class="w-full text-sm border"><thead class="bg-slate-100"><tr><th class="border p-2">节点</th><th class="border p-2">月配额</th><th class="border p-2">重置日</th><th class="border p-2">上报地址</th><th class="border p-2">当前累计</th><th class="border p-2">最后上报</th><th class="border p-2">维护操作</th></tr></thead><tbody><tr v-if="rows.length===0"><td colspan="7" class="border p-3 text-center text-slate-500">暂无节点配置</td></tr><tr v-for="row in rows" :key="row.node_id"><td class="border p-2">{{row.node_id}}</td><td class="border p-2">{{row.monthly_quota_gb}}</td><td class="border p-2">{{row.reset_day}}</td><td class="border p-2">{{row.agent_endpoint}}</td><td class="border p-2">{{row.used}}</td><td class="border p-2">{{row.timestamp}}</td><td class="border p-2"><button @click="editNode(row)" class="bg-amber-500 text-white px-2 py-1 rounded">修改配置</button><button @click="deleteNode(row.node_id)" class="bg-rose-600 text-white px-2 py-1 rounded">删除节点</button></td></tr></tbody></table></div></div>
<div v-if="showAuth" class="fixed inset-0 bg-slate-900/50 flex items-center justify-center"><div class="bg-white rounded-xl p-6 w-80 space-y-3"><h3 class="text-lg font-bold">{{ authTitle }}</h3><input v-model="admin.username" placeholder="用户名" class="w-full border rounded px-3 py-2"><input v-model="admin.password" type="password" placeholder="密码" class="w-full border rounded px-3 py-2"><button @click="submitAuth" class="w-full bg-blue-600 text-white py-2 rounded">{{ authAction }}</button></div></div></div>
<script>
const {createApp}=Vue;createApp({data(){return{authInitialized:false,loggedIn:false,authSummary:'检测中...',showAuth:true,authMode:'init',admin:{username:'',password:''},form:{node_id:'demo-node',monthly_quota_gb:1024,reset_day:1,public_base_url:'',agent_endpoint:''},installCmd:'点击“生成安装命令”后显示...',centralUpgradeCmd:'点击“生成中心端升级命令”后显示...',outputText:'-',rows:[]}},computed:{authTitle(){return this.authMode==='init'?'首次初始化管理员':'管理员登录'},authAction(){return this.authMode==='init'?'初始化':'登录'}},methods:{async renderAuth(){const res=await fetch('/api/v1/admin/status');const data=await res.json();this.authInitialized=data.initialized;this.loggedIn=data.logged_in;this.authMode=!data.initialized?'init':'login';this.showAuth=!(data.initialized&&data.logged_in);this.authSummary=!data.initialized?'未初始化管理员':(data.logged_in?`已登录：${data.username}`:'未登录');},async submitAuth(){const api=this.authMode==='init'?'/api/v1/admin/init':'/api/v1/admin/login';const res=await fetch(api,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(this.admin)});if(!res.ok){alert(`${this.authAction}失败`);return;}await this.renderAuth();await this.loadDashboard();},async quickSetup(){const payload={...this.form,public_base_url:this.form.public_base_url.trim()||null,agent_endpoint:this.form.agent_endpoint.trim()||null};const res=await fetch('/api/v1/quick-setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await res.json();this.outputText=JSON.stringify(res.ok?data.config:data,null,2);if(res.ok){this.installCmd=data.install_command;await this.loadDashboard();}},genCentralUpgrade(){this.centralUpgradeCmd=`curl -fsSL '${window.location.origin}/api/v1/central/scripts/upgrade.sh' | sudo bash -s -- upgrade`;},async editNode(row){const monthly=prompt(`修改 ${row.node_id} 月配额(GB):`,row.monthly_quota_gb);if(!monthly){return;}const reset=prompt(`修改 ${row.node_id} 重置日(1-31):`,row.reset_day);if(!reset){return;}const payload={monthly_quota_gb:Number(monthly),reset_day:Number(reset),login_verify_enabled:row.login_verify_enabled,login_verify_token:row.login_verify_token,install_script_url:row.install_script_url,uninstall_script_url:row.uninstall_script_url,agent_endpoint:row.agent_endpoint,agent_api_key:row.agent_api_key,agent_hmac_secret:row.agent_hmac_secret,agent_iface:row.agent_iface,agent_interval:row.agent_interval};const res=await fetch(`/api/v1/nodes/${encodeURIComponent(row.node_id)}/config`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(!res.ok){alert('修改失败');return;}await this.loadDashboard();},async deleteNode(nodeId){if(!confirm(`确认直接删除节点 ${nodeId} 吗？`)){return;}const res=await fetch(`/api/v1/nodes/${encodeURIComponent(nodeId)}`,{method:'DELETE'});if(!res.ok){alert('删除失败');return;}await this.loadDashboard();},async loadDashboard(){const res=await fetch('/api/v1/dashboard');if(!res.ok){this.rows=[];return;}const data=await res.json();const latest=data.latest_ingest||{};this.rows=(data.nodes||[]).map(n=>{const li=latest[n.node_id]||{};return{...n,used:((Number(li.counters&&li.counters.rx_total_bytes||0)+Number(li.counters&&li.counters.tx_total_bytes||0))/1024/1024/1024>0)?`${((Number(li.counters&&li.counters.rx_total_bytes||0)+Number(li.counters&&li.counters.tx_total_bytes||0))/1024/1024/1024).toFixed(2)} GiB`:'-',timestamp:li.timestamp||'-'}});}},async mounted(){await this.renderAuth();await this.loadDashboard();}}).mount('#app');
</script></body></html>"""
@app.get("/api/v1/admin/status")
def admin_status(session: str | None = Cookie(default=None)):
    return {"initialized": bool(ADMIN_STATE["password_hash"]), "logged_in": bool(session in ADMIN_SESSIONS), "username": ADMIN_STATE["username"]}


@app.post("/api/v1/admin/init")
def admin_init(payload: AdminCreds):
    if ADMIN_STATE["password_hash"]:
        raise HTTPException(status_code=409, detail="already initialized")
    ADMIN_STATE["username"] = payload.username
    ADMIN_STATE["password_hash"] = _hash_password(payload.password)
    token = secrets.token_hex(24)
    ADMIN_SESSIONS.add(token)
    resp = Response(content=json.dumps({"ok": True}), media_type="application/json")
    resp.set_cookie("session", token, httponly=True, samesite="lax")
    return resp


@app.post("/api/v1/admin/login")
def admin_login(payload: AdminCreds):
    if payload.username != ADMIN_STATE["username"] or _hash_password(payload.password) != ADMIN_STATE["password_hash"]:
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = secrets.token_hex(24)
    ADMIN_SESSIONS.add(token)
    resp = Response(content=json.dumps({"ok": True}), media_type="application/json")
    resp.set_cookie("session", token, httponly=True, samesite="lax")
    return resp


@app.post("/api/v1/quick-setup")
def quick_setup(payload: QuickSetupRequest, request: Request, session: str | None = Cookie(default=None)):
    _require_admin(session)
    base = _external_base_url(request)
    public_base = str(payload.public_base_url).rstrip("/") if payload.public_base_url else _script_base_url(request)
    ingest_endpoint = str(payload.agent_endpoint) if payload.agent_endpoint else f"{base}/api/v1/ingest"
    node_id = payload.node_id.strip()
    node_id_token = "".join(ch if ch.isalnum() else "-" for ch in node_id).strip("-").lower() or "node"
    api_key = f"node-{node_id_token}-{secrets.token_hex(4)}"
    hmac_secret = secrets.token_hex(16)
    login_token = secrets.token_hex(12)

    cfg = NodeConfig(
        node_id=node_id,
        monthly_quota_gb=payload.monthly_quota_gb,
        reset_day=payload.reset_day,
        login_verify_enabled=True,
        login_verify_token=login_token,
        install_script_url=f"{public_base}/agent/traffic_agent.py",
        uninstall_script_url=f"{base}/api/v1/nodes/{node_id}/scripts/uninstall.sh",
        agent_endpoint=ingest_endpoint,
        agent_api_key=api_key,
        agent_hmac_secret=hmac_secret,
    )
    NODE_CONFIGS[node_id] = cfg
    NODE_SECRETS[api_key] = {"hmac_secret": hmac_secret, "node_id": node_id}

    install_cmd = (
        f"curl -fsSL '{public_base}/raw/{quote(cfg.agent_api_key, safe='')}/agent-bootstrap.sh' "
        f"| sudo NODE_ID={shlex.quote(node_id)} ENDPOINT={shlex.quote(cfg.agent_endpoint)} "
        f"API_KEY={shlex.quote(cfg.agent_api_key)} HMAC_SECRET={shlex.quote(cfg.agent_hmac_secret)} bash -s -- install"
    )
    return {"ok": True, "config": asdict(cfg), "install_command": install_cmd}


@app.get("/raw/{api_key}/agent-bootstrap.sh")
def raw_agent_bootstrap(api_key: str):
    if api_key not in NODE_SECRETS:
        raise HTTPException(status_code=404, detail="script not found")
    script_path = SCRIPTS_DIR / "agent-bootstrap.sh"
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="bootstrap script missing")
    script = script_path.read_text(encoding="utf-8")
    return Response(content=script, media_type="text/x-shellscript")


@app.get("/api/v1/nodes/{node_id}/config")
def get_node_config(node_id: str):
    cfg = NODE_CONFIGS.get(node_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="node not found")
    return asdict(cfg)


@app.put("/api/v1/nodes/{node_id}/config")
def update_node_config(node_id: str, update: ConfigUpdate):
    if node_id not in NODE_CONFIGS:
        raise HTTPException(status_code=404, detail="node not found")
    cfg = NodeConfig(node_id=node_id, **update.model_dump())
    NODE_CONFIGS[node_id] = cfg
    NODE_SECRETS[cfg.agent_api_key] = {"hmac_secret": cfg.agent_hmac_secret, "node_id": node_id}
    return {"ok": True, "config": asdict(cfg)}


@app.delete("/api/v1/nodes/{node_id}")
def delete_node_api(node_id: str, session: str | None = Cookie(default=None)):
    _require_admin(session)
    if not delete_node(node_id):
        raise HTTPException(status_code=404, detail="node not found")
    return {"ok": True, "node_id": node_id, "deleted": True}


@app.get("/api/v1/nodes/{node_id}/scripts/{action}.sh")
def get_node_script(node_id: str, action: str):
    cfg = NODE_CONFIGS.get(node_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="node not found")
    script = build_one_click_script(cfg, action)
    return Response(content=script, media_type="text/x-shellscript")


@app.post("/api/v1/nodes/{node_id}/login-verify")
def verify_node_login(node_id: str, payload: LoginVerifyRequest):
    cfg = NODE_CONFIGS.get(node_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="node not found")
    if not cfg.login_verify_enabled:
        return {"ok": True, "verify_enabled": False, "verified": True, "reason": "verification disabled"}
    verified = secrets.compare_digest(payload.token, cfg.login_verify_token)
    if not verified:
        raise HTTPException(status_code=401, detail="invalid login token")
    return {"ok": True, "verify_enabled": True, "verified": True}


@app.post("/api/v1/nodes/{node_id}/actions/uninstall")
def queue_uninstall_action(node_id: str, session: str | None = Cookie(default=None)):
    _require_admin(session)
    if not delete_node(node_id):
        raise HTTPException(status_code=404, detail="node not found")
    return {"ok": True, "node_id": node_id, "action": "uninstall", "deleted": True}


@app.get("/api/v1/nodes/{node_id}/actions/next")
def next_node_action(node_id: str, api_key: str):
    key_cfg = NODE_SECRETS.get(api_key)
    if not key_cfg or key_cfg["node_id"] != node_id:
        raise HTTPException(status_code=401, detail="invalid api key")
    action = NODE_PENDING_ACTIONS.pop(node_id, None)
    return {"ok": True, "action": action}


@app.get("/api/v1/central/scripts/upgrade.sh")
def get_central_upgrade_script():
    script = build_central_upgrade_script()
    return Response(content=script, media_type="text/x-shellscript")


@app.get("/api/v1/public-dashboard")
def public_dashboard():
    return {"nodes": [{"node_id": cfg.node_id, "monthly_quota_gb": cfg.monthly_quota_gb, "reset_day": cfg.reset_day, "agent_iface": cfg.agent_iface} for cfg in NODE_CONFIGS.values()], "latest_ingest": LATEST_INGEST}


@app.get("/api/v1/dashboard")
def dashboard(session: str | None = Cookie(default=None)):
    _require_admin(session)
    return {"nodes": [asdict(cfg) for cfg in NODE_CONFIGS.values()], "latest_ingest": LATEST_INGEST}


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
    normalized_ts = x_timestamp.replace("Z", "+00:00")
    ts = int(datetime.fromisoformat(normalized_ts).astimezone(timezone.utc).timestamp())
    if abs(now - ts) > 300:
        raise HTTPException(status_code=401, detail="timestamp outside 5-minute window")

    dedupe_key = f"{payload.node_id}:{x_timestamp}:{x_nonce}"
    if dedupe_key in INGEST_CACHE:
        return {"ok": True, "deduped": True}

    body = json.dumps(payload.model_dump(), separators=(",", ":")).encode()
    if not verify_sig(key_cfg["hmac_secret"], x_timestamp, x_nonce, body, x_signature):
        raise HTTPException(status_code=401, detail="bad signature")

    INGEST_CACHE.add(dedupe_key)
    LATEST_INGEST[payload.node_id] = {
        "timestamp": payload.timestamp,
        "iface": payload.iface,
        "counters": payload.counters,
        "hostname": payload.hostname,
        "agent_version": payload.agent_version,
    }
    return {"ok": True, "stored": True}
