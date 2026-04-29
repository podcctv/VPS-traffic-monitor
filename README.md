# VPS Traffic Monitor MVP

## 快速说明（GitHub 项目直接构建镜像）

- 中心端支持直接在 GitHub 项目目录中构建镜像并启动。
- 一键安装脚本场景下，中心端只需要准备并分发 `docker-compose.yml`，即可快速一键安装。
- 首次安装完成后，请先在系统中设置登录用户名和密码。
- 默认未登录页面仅支持查看 VPS 流量，不提供修改权限；登录后可进行相关配置并进入后台管理。

## 1) 中心端一键安装（Docker）

### 1.1 前置要求

- 一台可访问公网的服务器（建议 Ubuntu 20.04+）
- 已安装 Docker 与 Docker Compose（`docker compose` 命令可用）
- 已放行中心端端口（默认 `8000`，建议配合 Nginx + HTTPS 暴露）

### 1.2 一键启动中心端

```bash
docker compose up -d --build
```

### 1.3 验证中心端是否启动成功

```bash
curl -s http://127.0.0.1:8000/docs >/dev/null && echo ok
```

若返回 `ok`，说明中心端运行正常。

## 2) 在中心端配置 Agent 节点参数

先调用配置接口写入 Agent 参数（示例节点：`demo-node`）：

```bash
curl -X PUT 'http://127.0.0.1:8000/api/v1/nodes/demo-node/config' \
  -H 'Content-Type: application/json' \
  -d '{
    "monthly_quota_gb": 1024,
    "reset_day": 1,
    "login_verify_enabled": true,
    "install_script_url": "https://your-central.example.com/agent/traffic_agent.py",
    "uninstall_script_url": "https://your-central.example.com/api/v1/nodes/demo-node/scripts/uninstall.sh",
    "agent_endpoint": "https://your-central.example.com/api/v1/ingest",
    "agent_api_key": "demo-key",
    "agent_hmac_secret": "demo-secret",
    "agent_iface": "eth0",
    "agent_interval": 120
  }'
```

> `agent_endpoint` / `install_script_url` / `uninstall_script_url` 建议全部使用 HTTPS。

## 3) 生成 Agent 一键安装/卸载脚本（由中心端动态生成）

中心端会按节点配置动态生成脚本：

- 安装脚本：`GET /api/v1/nodes/{node_id}/scripts/install.sh`
- 卸载脚本：`GET /api/v1/nodes/{node_id}/scripts/uninstall.sh`

下载示例：

```bash
curl -fsSL 'http://127.0.0.1:8000/api/v1/nodes/demo-node/scripts/install.sh' -o install.sh
curl -fsSL 'http://127.0.0.1:8000/api/v1/nodes/demo-node/scripts/uninstall.sh' -o uninstall.sh
```

## 4) Agent 端一键安装与配置（在目标 VPS 执行）

> 以下命令在 **Agent 目标 VPS** 上执行，不在中心端服务器执行。

### 4.1 一键安装 Agent

```bash
curl -fsSL 'https://your-central.example.com/api/v1/nodes/demo-node/scripts/install.sh' | sudo bash -s -- install
```

安装脚本会自动完成：

- 创建并写入 Agent 配置（endpoint、api-key、hmac、网卡、上报间隔等）
- 安装/更新运行文件
- 启动并设置服务为开机自启（若系统支持 systemd）

### 4.2 查看 Agent 运行状态（可选）

```bash
sudo systemctl status traffic-agent --no-pager
```

```bash
sudo journalctl -u traffic-agent -n 100 --no-pager
```

### 4.3 一键卸载 Agent

```bash
curl -fsSL 'https://your-central.example.com/api/v1/nodes/demo-node/scripts/uninstall.sh' | sudo bash -s -- uninstall
```

## 5) 推荐的一键安装完整流程（中心端 + Agent 端）

1. 在中心端服务器执行 `docker compose up -d --build` 启动服务。  
2. 通过中心端 API 写入节点配置（配额、密钥、上报地址、脚本地址等）。  
3. 在目标 VPS 运行中心端生成的 `install.sh` 完成 Agent 一键安装。  
4. 回到中心端页面确认节点是否开始上报流量。  
5. 需要下线节点时，在目标 VPS 执行 `uninstall.sh` 一键卸载。  

## Agent（手动运行模式）

```bash
python3 agent/traffic_agent.py \
  --endpoint http://127.0.0.1:8000/api/v1/ingest \
  --api-key demo-key \
  --hmac-secret demo-secret \
  --node-id demo-node \
  --iface eth0
```

周期上报：加上 `--interval 120`。

## 中心端（非 Docker）

安装依赖：

```bash
pip install fastapi uvicorn
```

启动：

```bash
uvicorn central.server:app --host 0.0.0.0 --port 8000
```
