# VPS Traffic Monitor MVP

## 极简目标（你现在可以这样用）

中心端通过 Docker 一键安装后，你登录网页只需要做 3 件事：

1. 填写节点 ID
2. 填写节点月流量（GB）
3. 填写每月重置日期（1-31）

点击 **「一键生成安装命令」**，系统会自动：

- 生成并保存该节点完整配置
- 自动生成 API Key/HMAC 密钥
- 自动拼好 Agent 上报地址与脚本地址
- 给出可直接复制的 VPS 安装命令

---

## 1) 中心端一键安装（Docker）

```bash
docker compose up -d --build
```

验证：

```bash
curl -s http://127.0.0.1:8000/docs >/dev/null && echo ok
```

---

## 2) 网页极简配置（推荐）

打开：`http://你的中心端IP:8000/`

在首页输入：

- 节点 ID
- 月流量配额（GB）
- 重置日

点击 **一键生成安装命令**，复制命令到目标 VPS 执行即可。

---

## 3) Agent 一键安装（目标 VPS 执行）

示例（以网页生成结果为准）：

```bash
curl -fsSL 'https://your-central.example.com/api/v1/nodes/demo-node/scripts/install.sh' | sudo bash -s -- install
```

可选卸载：

```bash
curl -fsSL 'https://your-central.example.com/api/v1/nodes/demo-node/scripts/uninstall.sh' | sudo bash -s -- uninstall
```

---

## 4) API 方式（可选）

如果你不走网页，也可以调这个极简接口：

`POST /api/v1/quick-setup`

```json
{
  "node_id": "demo-node",
  "monthly_quota_gb": 1024,
  "reset_day": 1
}
```

返回包含：

- `config`：完整节点配置
- `install_command`：可直接执行的一键安装命令

---

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

```bash
pip install fastapi uvicorn
uvicorn central.server:app --host 0.0.0.0 --port 8000
```
