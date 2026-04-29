# VPS Traffic Monitor

一个用于 **VPS 流量采集、上报与可视化** 的轻量级项目：
- **Central（中心端）**：提供配置管理、安装脚本生成、数据接收与展示页面。
- **Agent（节点端）**：部署在各 VPS 上，定时采集网卡流量并上报到中心端。

---

## 功能概览

- 节点配置管理（节点 ID、月流量配额、重置日）
- 自动生成节点安装/卸载脚本命令
- 流量数据上报接口（支持 API Key + HMAC）
- 登录校验接口（可按节点开关 + token 校验）
- Web 页面查看节点状态与用量
- Docker Compose 一键启动中心端

---

## 运行要求

- Linux 服务器（推荐 Ubuntu / Debian）
- Docker 24+
- Docker Compose Plugin

环境检查：

```bash
docker --version
docker compose version
```

---

## 快速开始（推荐）

> 当前 `docker-compose.yml` 使用 `build: .`，因此**必须先克隆完整仓库**再启动。

```bash
git clone https://github.com/podcctv/VPS-traffic-monitor.git
cd VPS-traffic-monitor
docker compose up -d --build
```

启动后可验证：

```bash
curl -sS http://127.0.0.1:8000/docs >/dev/null && echo "central ok"
```

浏览器访问：

- `http://<你的服务器IP>:8000/`

如果开启防火墙，请放行 `8000/tcp`。



## 中心端一键升级

可在中心端页面点击“生成中心端升级命令”，或直接执行：

```bash
curl -fsSL 'http://<你的服务器IP>:8000/api/v1/central/scripts/upgrade.sh' | sudo bash -s -- upgrade
```

支持环境变量：
- `REPO_URL`：仓库地址（默认 `https://github.com/podcctv/VPS-traffic-monitor.git`）
- `INSTALL_DIR`：部署目录（默认 `/opt/VPS-traffic-monitor`）
- `BRANCH`：升级分支（默认 `main`）

该脚本会自动更新仓库并执行 `docker compose up -d --build --remove-orphans`。

如果你希望本地持久化一个“可自我更新”的升级脚本，可先保存后执行：

```bash
curl -fsSL 'http://<你的服务器IP>:8000/api/v1/central/scripts/upgrade.sh' -o /usr/local/bin/vtm-central-upgrade
chmod +x /usr/local/bin/vtm-central-upgrade
/usr/local/bin/vtm-central-upgrade upgrade
```

后续每次运行时会尝试刷新 `/usr/local/bin/vtm-central-upgrade` 本身。


---

## 节点安装（Agent）

在中心端页面填写节点信息后，可获得一键安装命令。示例：

```bash
curl -fsSL 'https://your-central.example.com/api/v1/nodes/demo-node/scripts/install.sh' | sudo bash -s -- install
```

卸载示例：

```bash
curl -fsSL 'https://your-central.example.com/api/v1/nodes/demo-node/scripts/uninstall.sh' | sudo bash -s -- uninstall
```

---

## API 使用（可选）

可通过 `POST /api/v1/quick-setup` 创建节点配置并获取安装命令。

请求示例：

```json
{
  "node_id": "demo-node",
  "monthly_quota_gb": 1024,
  "reset_day": 1
}
```

返回内容包含：
- `config`：节点完整配置
- `install_command`：一键安装命令

新增接口：
- `POST /api/v1/nodes/{node_id}/login-verify`：登录验证
- `GET /api/v1/dashboard`：中心端展示数据（节点配置 + 最新上报）

---

## 本地开发运行

### 1) 启动中心端（非 Docker）

```bash
pip install fastapi uvicorn
uvicorn central.server:app --host 0.0.0.0 --port 8000
```

### 2) 手动运行 Agent

```bash
python3 agent/traffic_agent.py \
  --endpoint http://127.0.0.1:8000/api/v1/ingest \
  --api-key demo-key \
  --hmac-secret demo-secret \
  --node-id demo-node \
  --iface eth0 \
  --interval 120
```

---

## 目录结构

```text
.
├── agent/
│   └── traffic_agent.py
├── central/
│   └── server.py
├── docker-compose.yml
├── Dockerfile
└── README.md
```

---

## 常见问题

### 1) 为什么不能只下载 `docker-compose.yml` 直接启动？

因为当前 compose 配置使用 `build: .`，需要本地存在 `Dockerfile` 和项目源码作为构建上下文。

### 2) 生产环境建议

- 使用 HTTPS 暴露中心端
- 将 API Key / HMAC Secret 设置为高强度随机值
- 配置反向代理与基础访问控制
