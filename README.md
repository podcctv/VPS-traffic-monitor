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

## 3. 快速启动（新架构）

### 3.1 配置环境变量（建议）

```bash
cp .env.example .env 2>/dev/null || true
# 至少设置：INFLUXDB_TOKEN, INFLUXDB_PASSWORD
```

你也可以直接通过 shell 导出：

```bash
export INFLUXDB_TOKEN='replace-with-strong-token'
export INFLUXDB_PASSWORD='replace-with-strong-password'
export INFLUXDB_ORG='vtm'
export INFLUXDB_BUCKET='traffic_raw'
```

### 3.2 启动服务

```bash
docker compose up -d --build
```

### 3.3 验证

```bash
# 中心 API
curl -fsS http://127.0.0.1:8000/docs >/dev/null && echo 'central-api ok'

# InfluxDB
curl -fsS http://127.0.0.1:8086/health

# Grafana
curl -fsS http://127.0.0.1:3000/api/health
```

默认端口：
- `8000`：Central API
- `8086`：InfluxDB
- `3000`：Grafana

---

## 4. 节点端接入

### 4.1 保留现有 Python Agent（兼容模式）

```bash
python3 agent/traffic_agent.py \
  --endpoint http://<central-host>:8000/api/v1/ingest \
  --api-key <api-key> \
  --hmac-secret <hmac-secret> \
  --node-id <node-id> \
  --iface all \
  --interval 120
```

### 4.2 推荐迁移到 Telegraf（生产建议）

建议采用双写验证：
1. 维持现有 Agent 上报；
2. 并行部署 Telegraf；
3. 对比 24h 数据偏差（目标 < 1~2%）；
4. 切换主链路到 Telegraf。

详细步骤见：`ARCHITECTURE_MIGRATION_CN.md`。

---

## 5. 数据分层建议（中心端）

- `raw_metrics`：1~5 分钟粒度原始计数；
- `agg_hourly/agg_daily`：小时/天聚合；
- `billing_usage`：账期口径（baseline + 用量）。

核心原则：**页面优先读聚合层和账期层，不直接扫描 raw 全量数据。**

---

## 6. 生产部署建议

- 全站 HTTPS（建议反向代理 + TLS）；
- API Key/HMAC Secret 定期轮换；
- InfluxDB token 最小权限化；
- 对外仅暴露必要端口（通常只暴露 80/443，内部服务走内网）；
- 增加备份策略（InfluxDB + Redis 持久化卷）。

---

## 7. 路线图

- [x] 基础设施改造为 InfluxDB + Redis + Grafana + Central API
- [x] 兼容现有 Agent 上报链路
- [ ] 中心端账期引擎（baseline/reset/counter reset）
- [ ] 聚合任务（hourly/daily）
- [ ] 自定义业务展示页（节点总览/详情/告警）

