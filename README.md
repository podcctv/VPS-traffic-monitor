# VPS Traffic Monitor MVP

## 先回答：当前 `docker-compose.yml` 能不能直接运行？

**结论：不能直接按注释里的“一键拉取并启动”方式运行。**

原因：当前 compose 使用的是 `build: .`，这要求你本地有完整项目源码目录；但注释中的方式只是下载一个 `docker-compose.yml` 文件，没有 `Dockerfile` 与源码上下文，因此构建会失败。

---

## 安装方式 A（推荐）：从 Git 拉取后启动

适用于你自己部署、最稳妥也最容易排查问题。

### 1）准备环境

- Linux 服务器（Ubuntu/Debian/CentOS 均可）
- 已安装 Docker + Docker Compose Plugin

快速检查：

```bash
docker --version
docker compose version
```

### 2）拉取项目

```bash
git clone https://github.com/<your-org>/<your-repo>.git
cd <your-repo>
```

### 3）启动中心端

```bash
docker compose up -d --build
```

### 4）验证服务

```bash
curl -sS http://127.0.0.1:8000/docs >/dev/null && echo "central ok"
```

如果服务器开了防火墙，请放行 `8000/tcp`。

### 5）访问页面

浏览器打开：

`http://你的服务器IP:8000/`

然后按页面提示：填写节点 ID、月流量、重置日，点击**一键生成安装命令**即可。

---

## 安装方式 B：仅下载 compose 一键启动（前提：你已发布镜像）

只有在你把镜像发布到仓库（例如 GHCR / Docker Hub）并把 `docker-compose.yml` 中 `image:` 改成真实地址后，这种方式才可用。

```bash
curl -fsSL https://raw.githubusercontent.com/<your-org>/<your-repo>/main/docker-compose.yml -o docker-compose.yml \
  && docker compose -f docker-compose.yml pull \
  && docker compose -f docker-compose.yml up -d
```

---

## Agent 一键安装（目标 VPS 执行）

示例（以网页生成结果为准）：

```bash
curl -fsSL 'https://your-central.example.com/api/v1/nodes/demo-node/scripts/install.sh' | sudo bash -s -- install
```

可选卸载：

```bash
curl -fsSL 'https://your-central.example.com/api/v1/nodes/demo-node/scripts/uninstall.sh' | sudo bash -s -- uninstall
```

---

## API 方式（可选）

如果你不走网页，也可以调这个接口：

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
