# VPS Traffic Monitor（新架构版）

本项目已切换为 **“节点本地采集 + 中心端时序存储 + 业务展示层”** 架构，目标是：
- 采集链路尽量使用成熟组件；
- 中心端只做你关心的流量业务视图与账期逻辑；
- 允许后续平滑扩展到更多节点和更复杂统计。

---

## 1. 新架构概览

```text
[VPS Node]
  vnstatd / telegraf
      ↓
  traffic_agent.py（兼容采集上报）
      ↓ HTTPS + API Key + HMAC
[Center]
  FastAPI (central-api)
      ├── InfluxDB (raw_metrics 时序数据)
      ├── Redis (在线状态/短缓存)
      └── billing/aggregation（账期与聚合，API层逐步承接）
      ↓
  Grafana（运维视图） + 自定义前端（业务视图）
```

> 当前仓库已完成基础设施层切换（InfluxDB/Redis/Grafana/中心 API 编排），并保留现有 Agent 上报兼容能力。

---

## 2. 目录结构

```text
.
├── agent/
│   └── traffic_agent.py
├── central/
│   └── server.py
├── docker-compose.yml
├── Dockerfile
├── ARCHITECTURE_MIGRATION_CN.md
└── README.md
```

---

## 3. 全新安装（从 0 到可用）

> 适用于第一次部署，或准备在一台全新 Linux 服务器（建议 Ubuntu 22.04+/Debian 12+）上安装。

### 3.1 前置条件

- 一台可联网的 Linux 服务器（建议 `2C4G` 起步）；
- 已安装 Git；
- 已安装 Docker Engine（建议 24+）；
- 已安装 Docker Compose Plugin（支持 `docker compose` 命令）；
- 防火墙放行中心端口（最少 `8000/8086/3000`，生产建议仅暴露 `80/443` 反向代理）。

可用以下命令自检：

```bash
git --version
docker --version
docker compose version
```

### 3.2 拉取代码

```bash
git clone https://github.com/podcctv/VPS-traffic-monitor.git
cd VPS-traffic-monitor
```

### 3.3 创建环境变量文件

项目目前未内置 `.env.example`，请手动创建 `.env`：

```bash
cat > .env <<'ENV'
INFLUXDB_TOKEN=replace-with-strong-token
INFLUXDB_PASSWORD=replace-with-strong-password
INFLUXDB_ORG=vtm
INFLUXDB_BUCKET=traffic_raw
ENV
```

> 建议把 `INFLUXDB_TOKEN` 与 `INFLUXDB_PASSWORD` 设置为长度 24+ 的高强度随机字符串。

### 3.4 首次启动

```bash
docker compose up -d --build
```

首次启动会拉取镜像并构建中心 API，耗时取决于网络环境。

### 3.5 健康检查

```bash
# 容器状态
docker compose ps

# 中心 API 文档
curl -fsS http://127.0.0.1:8000/docs >/dev/null && echo 'central-api ok'

# InfluxDB
curl -fsS http://127.0.0.1:8086/health

# Grafana
curl -fsS http://127.0.0.1:3000/api/health
```

### 3.6 默认账号与端口

默认端口：
- `8000`：Central API
- `8086`：InfluxDB
- `3000`：Grafana

默认账号（仅本地初始化用途，请上线前修改）：
- Grafana: `admin / admin`

### 3.7 升级与重建

```bash
# 拉取最新代码
git pull

# 重建并滚动更新
docker compose up -d --build
```

如需完全重置（会删除容器和匿名卷，请谨慎）：

```bash
docker compose down -v
```

---

## 4. 快速启动（已有环境）

### 4.1 配置环境变量（建议）

如果你已经有外部密钥管理系统，也可以直接通过 shell 导出：

```bash
export INFLUXDB_TOKEN='replace-with-strong-token'
export INFLUXDB_PASSWORD='replace-with-strong-password'
export INFLUXDB_ORG='vtm'
export INFLUXDB_BUCKET='traffic_raw'
```

### 4.2 启动服务

```bash
docker compose up -d --build
```

### 4.3 验证

```bash
# 中心 API
curl -fsS http://127.0.0.1:8000/docs >/dev/null && echo 'central-api ok'

# InfluxDB
curl -fsS http://127.0.0.1:8086/health

# Grafana
curl -fsS http://127.0.0.1:3000/api/health
```

---

## 5. 节点端接入

### 5.1 保留现有 Python Agent（兼容模式）

```bash
python3 agent/traffic_agent.py \
  --endpoint http://<central-host>:8000/api/v1/ingest \
  --api-key <api-key> \
  --hmac-secret <hmac-secret> \
  --node-id <node-id> \
  --iface all \
  --interval 120
```

### 5.3 Agent 一键部署 / 一键卸载

中心端 Quick Setup 会返回一键安装命令（`install_command`），节点执行后会自动安装依赖、写入 systemd 服务并启动上报。

如需手工执行脚本，也可使用以下动作别名：

```bash
# 一键部署（等价于 install）
bash /usr/local/bin/vtm-agent deploy

# 一键卸载（等价于 uninstall）
bash /usr/local/bin/vtm-agent remove
```


### 5.4 查看 Agent 上报日志（排查是否成功上报）

> `systemctl status` 只显示服务启停，不会展示每次上报详情。

```bash
# 实时查看 Agent 业务日志（包含 upload status / upload failed）
tail -f /var/log/vps-traffic-monitor/agent.log

# 查看最近 200 行
tail -n 200 /var/log/vps-traffic-monitor/agent.log

# 同时看 systemd 事件
journalctl -u vps-traffic-agent.service -n 100 --no-pager
```

日志关键字：
- `upload status=200`：上报成功；
- `upload failed:`：上报失败；
- `upload skipped: no traffic change`：本轮流量无变化，按策略跳过。

### 5.2 推荐迁移到 Telegraf（生产建议）

建议采用双写验证：
1. 维持现有 Agent 上报；
2. 并行部署 Telegraf；
3. 对比 24h 数据偏差（目标 < 1~2%）；
4. 切换主链路到 Telegraf。

详细步骤见：`ARCHITECTURE_MIGRATION_CN.md`。

---

## 6. 数据分层建议（中心端）

- `raw_metrics`：1~5 分钟粒度原始计数；
- `agg_hourly/agg_daily`：小时/天聚合；
- `billing_usage`：账期口径（baseline + 用量）。

核心原则：**页面优先读聚合层和账期层，不直接扫描 raw 全量数据。**

---

## 7. 生产部署建议

- 全站 HTTPS（建议反向代理 + TLS）；
- API Key/HMAC Secret 定期轮换；
- InfluxDB token 最小权限化；
- 对外仅暴露必要端口（通常只暴露 80/443，内部服务走内网）；
- 增加备份策略（InfluxDB + Redis 持久化卷）。

---

## 8. 路线图

- [x] 基础设施改造为 InfluxDB + Redis + Grafana + Central API
- [x] 兼容现有 Agent 上报链路
- [ ] 中心端账期引擎（baseline/reset/counter reset）
- [ ] 聚合任务（hourly/daily）
- [ ] 自定义业务展示页（节点总览/详情/告警）
