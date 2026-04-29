# VPS Traffic Monitor MVP

## 快速说明（GitHub 项目直接构建镜像）

- 中心端支持直接在 GitHub 项目目录中构建镜像并启动。
- 一键安装脚本场景下，中心端只需要准备并分发 `docker-compose.yml`，即可快速一键安装。
- 首次安装完成后，请先在系统中设置登录用户名和密码。
- 默认未登录页面仅支持查看 VPS 流量，不提供修改权限；登录后可进行相关配置并进入后台管理。

## 1) 中心端 Docker 部署

```bash
docker compose up -d --build
```

健康检查：

```bash
curl -s http://127.0.0.1:8000/docs >/dev/null && echo ok
```

## 2) 中心端配置节点 Agent 参数

先调用配置接口写入 agent 参数（示例 node: `demo-node`）：

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

> `agent_endpoint/install_script_url/uninstall_script_url` 需使用 HTTPS。

## 3) 生成一键安装/卸载脚本

中心端会按节点配置动态生成脚本：

- 安装脚本：`GET /api/v1/nodes/{node_id}/scripts/install.sh`
- 卸载脚本：`GET /api/v1/nodes/{node_id}/scripts/uninstall.sh`

下载示例：

```bash
curl -fsSL 'http://127.0.0.1:8000/api/v1/nodes/demo-node/scripts/install.sh' -o install.sh
curl -fsSL 'http://127.0.0.1:8000/api/v1/nodes/demo-node/scripts/uninstall.sh' -o uninstall.sh
```

## 4) VPS 上一键安装 / 一键卸载

> 以下在 **目标 VPS** 执行。

一键安装：

```bash
curl -fsSL 'https://your-central.example.com/api/v1/nodes/demo-node/scripts/install.sh' | sudo bash -s -- install
```

一键卸载：

```bash
curl -fsSL 'https://your-central.example.com/api/v1/nodes/demo-node/scripts/uninstall.sh' | sudo bash -s -- uninstall
```

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

### 一键安装/卸载（由中心端下发）
中心端先在节点配置中写入脚本链接（必须是 HTTPS）：

- `install_script_url`
- `uninstall_script_url`

agent 端执行一键安装：

```bash
python3 agent/traffic_agent.py \
  --endpoint https://your-central.example.com/api/v1/ingest \
  --api-key demo-key \
  --hmac-secret demo-secret \
  --node-id demo-node \
  --one-click install
```

agent 端执行一键卸载：

```bash
python3 agent/traffic_agent.py \
  --endpoint https://your-central.example.com/api/v1/ingest \
  --api-key demo-key \
  --hmac-secret demo-secret \
  --node-id demo-node \
  --one-click uninstall
```

## 中心端
安装依赖：

```bash
pip install fastapi uvicorn
```

启动：

```bash
uvicorn central.server:app --host 0.0.0.0 --port 8000
```

### 节点配置接口
- `GET /api/v1/nodes/{node_id}/config`
- `PUT /api/v1/nodes/{node_id}/config`

可配置项：
- `monthly_quota_gb`：月总流量（GB）
- `reset_day`：每月重置日期（1-31）
- `login_verify_enabled`：是否开启登录验证
- `install_script_url`：agent 一键安装脚本地址（仅支持 HTTPS）
- `uninstall_script_url`：agent 一键卸载脚本地址（仅支持 HTTPS）
