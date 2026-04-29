# VPS Traffic Monitor MVP

## Agent
运行一次上报：

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
