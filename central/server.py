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
    agent_iface: str = "all"
    agent_interval: int = 120


NODE_CONFIGS: Dict[str, NodeConfig] = {}
NODE_SECRETS: Dict[str, dict] = {"demo-key": {"hmac_secret": "demo-secret", "node_id": "demo-node"}}
INGEST_CACHE = set()
LATEST_INGEST: Dict[str, dict] = {}
NODE_PENDING_ACTIONS: Dict[str, str] = {}
ADMIN_STATE = {"username": None, "password_hash": None}
ADMIN_SESSIONS = set()

ADMIN_STATE_FILE = BASE_DIR / "data" / "admin_state.json"


def _load_admin_state() -> None:
    if not ADMIN_STATE_FILE.exists():
        return
    try:
        raw = json.loads(ADMIN_STATE_FILE.read_text(encoding="utf-8"))
        ADMIN_STATE["username"] = raw.get("username")
        ADMIN_STATE["password_hash"] = raw.get("password_hash")
    except Exception:
        return


def _save_admin_state() -> None:
    ADMIN_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ADMIN_STATE_FILE.write_text(json.dumps(ADMIN_STATE, ensure_ascii=False), encoding="utf-8")


_load_admin_state()


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
    agent_iface: str = Field(default="all", min_length=1)
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
    interfaces: list[dict] | None = None


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
ExecStart=/usr/bin/python3 -u /opt/vps-traffic-monitor/traffic_agent.py \\
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
# Try pulling prebuilt image first; if registry access is denied, fallback to local build.
if ! docker compose pull; then
  echo "docker compose pull failed, fallback to local build" >&2
fi
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


def _node_health(node_id: str) -> dict:
    cfg = NODE_CONFIGS.get(node_id)
    if not cfg:
        return {"ok": False, "status": "missing", "message": "节点不存在"}
    latest = LATEST_INGEST.get(node_id)
    if not latest:
        return {"ok": False, "status": "never_reported", "message": "未收到任何上报：请先检查节点 Agent 服务是否运行"}
    ts_raw = latest.get("timestamp")
    try:
        dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return {"ok": False, "status": "bad_timestamp", "message": f"最近上报时间格式异常: {ts_raw}"}
    age = int(time.time() - dt.timestamp())
    if age > max(cfg.agent_interval * 3, 600):
        return {"ok": False, "status": "stale", "message": f"最近上报距今 {age}s，疑似 Agent 停止或网络异常", "last_report_at": dt.isoformat().replace("+00:00", "Z"), "age_seconds": age}
    return {"ok": True, "status": "healthy", "message": f"Agent 正常，最近上报 {age}s 前", "last_report_at": dt.isoformat().replace("+00:00", "Z"), "age_seconds": age}


@app.get("/", response_class=HTMLResponse)
def public_page():
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>VPS 流量面板</title><script src="https://cdn.tailwindcss.com"></script><script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script></head>
<body class="min-h-screen bg-slate-950 text-slate-100"><div id="app" class="max-w-6xl mx-auto p-6 space-y-4">
<div class="flex justify-between"><h1 class="text-2xl font-bold">VPS 流量面板</h1><div class="space-x-2"><a href="/admin" class="px-3 py-2 rounded bg-cyan-500 text-slate-900">后台</a><button @click="load" class="px-3 py-2 rounded bg-fuchsia-500">刷新</button></div></div>
<div class="grid gap-3"><div v-for="row in rows" :key="row.node_id" class="rounded border border-slate-700 p-4"><div class="flex justify-between"><div><p class="text-lg font-bold">{{row.node_id}}</p><p class="text-xs text-slate-400">主网卡 {{row.main_iface}}</p></div><a :href="'/node/'+encodeURIComponent(row.node_id)" class="text-cyan-300 text-sm">详情</a></div><p class="text-2xl font-bold text-emerald-300">{{row.used}}</p></div></div></div>
<script>const {createApp}=Vue;createApp({data(){return{rows:[]}},methods:{async load(){const r=await fetch('/api/v1/public-dashboard');if(!r.ok)return;const d=await r.json();const latest=d.latest_ingest||{};this.rows=(d.nodes||[]).map(n=>{const li=latest[n.node_id]||{};const ifs=(li.interfaces||[]);const m=n.agent_iface&&n.agent_iface!=='all'?n.agent_iface:(li.iface||'all');const mrow=ifs.find(i=>i.name===m);const c=(mrow?mrow.counters:li.counters)||{};const g=(Number(c.rx_total_bytes||0)+Number(c.tx_total_bytes||0))/1024/1024/1024;return {node_id:n.node_id,main_iface:m,used:`${g.toFixed(2)} GiB`};});}},mounted(){this.load();setInterval(this.load,15000)}}).mount('#app');</script></body></html>"""


@app.get("/node/{node_id}", response_class=HTMLResponse)
def node_detail_page(node_id: str):
    return f"""<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{node_id} 详情</title><script src=\"https://cdn.tailwindcss.com\"></script><script src=\"https://unpkg.com/vue@3/dist/vue.global.prod.js\"></script></head><body class=\"bg-slate-950 text-slate-100\"><div id=\"app\" class=\"max-w-5xl mx-auto p-6 space-y-4\"><a href=\"/\" class=\"text-cyan-300\">← 返回</a><h1 class=\"text-2xl font-bold\">节点 {node_id}（vnStat 风格）</h1><pre class=\"bg-slate-900 p-4 rounded overflow-auto\">{{{{summary}}}}</pre><div class=\"grid md:grid-cols-2 gap-4\"><div><h3 class=\"font-bold mb-2\">hourly</h3><table class=\"w-full text-sm\"><tr><th>time</th><th>rx</th><th>tx</th></tr><tr v-for=\"h in hourly\"><td>{{{{h.time||h.date||'-'}}}}</td><td>{{{{fmt(h.rx)}}}}</td><td>{{{{fmt(h.tx)}}}}</td></tr></table></div><div><h3 class=\"font-bold mb-2\">daily</h3><table class=\"w-full text-sm\"><tr><th>date</th><th>rx</th><th>tx</th></tr><tr v-for=\"x in daily\"><td>{{{{x.date||x.time||'-'}}}}</td><td>{{{{fmt(x.rx)}}}}</td><td>{{{{fmt(x.tx)}}}}</td></tr></table></div></div></div><script>const nodeId={json.dumps(node_id)};const {{createApp}}=Vue;createApp({{data(){{return{{hourly:[],daily:[],summary:'loading...'}}}},methods:{{fmt(v){{return (Number(v||0)/1024/1024/1024).toFixed(2)+' GiB'}},async load(){{const r=await fetch('/api/v1/public-dashboard');const d=await r.json();const li=(d.latest_ingest||{{}})[nodeId]||{{}};this.hourly=li.hourly||[];this.daily=li.daily||[];const c=li.counters||{{}};this.summary=`iface: ${{li.iface||'-'}}
rx_total: ${{this.fmt(c.rx_total_bytes)}}
tx_total: ${{this.fmt(c.tx_total_bytes)}}`;}}}},mounted(){{this.load();}}}}).mount('#app');</script></body></html>"""


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
<div class="overflow-auto"><table class="w-full text-sm border"><thead class="bg-slate-100"><tr><th class="border p-2">节点</th><th class="border p-2">月配额</th><th class="border p-2">重置日</th><th class="border p-2">上报地址</th><th class="border p-2">当前累计</th><th class="border p-2">最后上报</th><th class="border p-2">维护操作</th></tr></thead><tbody><tr v-if="rows.length===0"><td colspan="7" class="border p-3 text-center text-slate-500">暂无节点配置</td></tr><tr v-for="row in rows" :key="row.node_id"><td class="border p-2">{{row.node_id}}</td><td class="border p-2">{{row.monthly_quota_gb}}</td><td class="border p-2">{{row.reset_day}}</td><td class="border p-2">{{row.agent_endpoint}}</td><td class="border p-2">{{row.used}}</td><td class="border p-2">{{row.timestamp}}</td><td class="border p-2"><button @click="editNode(row)" class="bg-amber-500 text-white px-2 py-1 rounded">修改配置</button><button @click="diagnoseNode(row.node_id)" class="bg-indigo-600 text-white px-2 py-1 rounded">诊断Agent</button><button @click="deleteNode(row.node_id)" class="bg-rose-600 text-white px-2 py-1 rounded">删除节点</button></td></tr></tbody></table></div></div>
<div v-if="showAuth" class="fixed inset-0 bg-slate-900/50 flex items-center justify-center"><div class="bg-white rounded-xl p-6 w-80 space-y-3"><h3 class="text-lg font-bold">{{ authTitle }}</h3><input v-model="admin.username" placeholder="用户名" class="w-full border rounded px-3 py-2"><input v-model="admin.password" type="password" placeholder="密码" class="w-full border rounded px-3 py-2"><button @click="submitAuth" class="w-full bg-blue-600 text-white py-2 rounded">{{ authAction }}</button></div></div></div>
<script>
const {createApp}=Vue;createApp({data(){return{authInitialized:false,loggedIn:false,authSummary:'检测中...',showAuth:true,authMode:'init',admin:{username:'',password:''},form:{node_id:'demo-node',monthly_quota_gb:1024,reset_day:1,public_base_url:'',agent_endpoint:''},installCmd:'点击“生成安装命令”后显示...',centralUpgradeCmd:'点击“生成中心端升级命令”后显示...',outputText:'-',rows:[]}},computed:{authTitle(){return this.authMode==='init'?'首次初始化管理员':'管理员登录'},authAction(){return this.authMode==='init'?'初始化':'登录'}},methods:{async renderAuth(){const res=await fetch('/api/v1/admin/status');const data=await res.json();this.authInitialized=data.initialized;this.loggedIn=data.logged_in;this.authMode=!data.initialized?'init':'login';this.showAuth=!(data.initialized&&data.logged_in);this.authSummary=!data.initialized?'未初始化管理员':(data.logged_in?`已登录：${data.username}`:'未登录');},async submitAuth(){const api=this.authMode==='init'?'/api/v1/admin/init':'/api/v1/admin/login';const res=await fetch(api,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(this.admin)});if(!res.ok){alert(`${this.authAction}失败`);return;}await this.renderAuth();await this.loadDashboard();},async quickSetup(){const payload={...this.form,public_base_url:this.form.public_base_url.trim()||null,agent_endpoint:this.form.agent_endpoint.trim()||null};const res=await fetch('/api/v1/quick-setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await res.json();this.outputText=JSON.stringify(res.ok?data.config:data,null,2);if(res.ok){this.installCmd=data.install_command;await this.loadDashboard();}},genCentralUpgrade(){this.centralUpgradeCmd=`curl -fsSL '${window.location.origin}/api/v1/central/scripts/upgrade.sh' | sudo bash -s -- upgrade`;},async editNode(row){const monthly=prompt(`修改 ${row.node_id} 月配额(GB):`,row.monthly_quota_gb);if(!monthly){return;}const reset=prompt(`修改 ${row.node_id} 重置日(1-31):`,row.reset_day);if(!reset){return;}const iface=prompt(`修改 ${row.node_id} 主网卡(如 eth0 / ens3 / all):`,row.agent_iface||'all');if(!iface){return;}const payload={monthly_quota_gb:Number(monthly),reset_day:Number(reset),login_verify_enabled:row.login_verify_enabled,login_verify_token:row.login_verify_token,install_script_url:row.install_script_url,uninstall_script_url:row.uninstall_script_url,agent_endpoint:row.agent_endpoint,agent_api_key:row.agent_api_key,agent_hmac_secret:row.agent_hmac_secret,agent_iface:iface,agent_interval:row.agent_interval};const res=await fetch(`/api/v1/nodes/${encodeURIComponent(row.node_id)}/config`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(!res.ok){alert('修改失败');return;}await this.loadDashboard();},async deleteNode(nodeId){if(!confirm(`确认直接删除节点 ${nodeId} 吗？`)){return;}const res=await fetch(`/api/v1/nodes/${encodeURIComponent(nodeId)}`,{method:'DELETE'});if(!res.ok){alert('删除失败');return;}await this.loadDashboard();},async diagnoseNode(nodeId){const res=await fetch(`/api/v1/nodes/${encodeURIComponent(nodeId)}/health`);const data=await res.json();if(!res.ok){alert(`诊断失败: ${data.detail||res.status}`);return;}alert(`节点: ${nodeId}\n状态: ${data.status}\n结果: ${data.message}\n最近上报: ${data.last_report_at||'-'}`);},async loadDashboard(){const res=await fetch('/api/v1/dashboard');if(!res.ok){this.rows=[];return;}const data=await res.json();const latest=data.latest_ingest||{};this.rows=(data.nodes||[]).map(n=>{const li=latest[n.node_id]||{};return{...n,used:((Number(li.counters&&li.counters.rx_total_bytes||0)+Number(li.counters&&li.counters.tx_total_bytes||0))/1024/1024/1024>0)?`${((Number(li.counters&&li.counters.rx_total_bytes||0)+Number(li.counters&&li.counters.tx_total_bytes||0))/1024/1024/1024).toFixed(2)} GiB`:'-',timestamp:li.timestamp||'-'}});}},async mounted(){await this.renderAuth();await this.loadDashboard();}}).mount('#app');
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
    _save_admin_state()
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
        install_script_url=f"{base}/api/v1/nodes/{node_id}/scripts/install.sh",
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


@app.get("/raw/{api_key}/traffic_agent.py")
def raw_agent_python(api_key: str):
    if api_key not in NODE_SECRETS:
        raise HTTPException(status_code=404, detail="script not found")
    script_path = BASE_DIR / "agent" / "traffic_agent.py"
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="agent script missing")
    return Response(content=script_path.read_text(encoding="utf-8"), media_type="text/x-python")


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
    old_key = NODE_CONFIGS[node_id].agent_api_key
    cfg = NodeConfig(node_id=node_id, **update.model_dump())
    NODE_CONFIGS[node_id] = cfg
    if old_key != cfg.agent_api_key:
        NODE_SECRETS.pop(old_key, None)
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
    if node_id not in NODE_CONFIGS:
        raise HTTPException(status_code=404, detail="node not found")
    NODE_PENDING_ACTIONS[node_id] = "uninstall"
    return {"ok": True, "node_id": node_id, "action": "uninstall", "queued": True}


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




@app.get("/api/v1/nodes/{node_id}/health")
def node_health(node_id: str, session: str | None = Cookie(default=None)):
    _require_admin(session)
    return {"node_id": node_id, **_node_health(node_id)}

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
        "interfaces": payload.interfaces or [],
        "counters": payload.counters,
        "hostname": payload.hostname,
        "agent_version": payload.agent_version,
        "hourly": payload.hourly,
        "daily": payload.daily,
    }
    return {"ok": True, "stored": True}
