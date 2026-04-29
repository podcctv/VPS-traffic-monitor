# 中心化 VPS 流量统计方案（基于 vnStat）

## 1. 目标
- 每台 VPS 安装 `vnstat`，本地采集网卡流量。
- 由 Agent 将聚合后的统计数据（小时/天/月）上报到中心节点。
- 中心节点统一存储、展示、告警，并支持**按账期重置**（逻辑重置，不清空原始历史）。
- 页面风格参考赛博朋克（霓虹、网格、高对比）。

---

## 2. 总体架构

```text
[VPS-1..N]
  ├─ vnstatd (采集)
  └─ traffic-agent (上报)
         │ HTTPS + HMAC/mTLS
         ▼
[Central API]
  ├─ Ingest API (/api/v1/ingest)
  ├─ Auth/RateLimit/Replay防护
  ├─ Billing Engine (账期计算)
  └─ Query API (/api/v1/dashboard/*)
         │
         ├─ PostgreSQL (元数据 + 聚合结果 + 账期快照)
         ├─ Redis (缓存 + 在线状态)
         └─ WebSocket/SSE (实时推送)
                 ▼
            [Web Dashboard]
```

### 组件职责
1. **VPS 侧**
   - `vnstatd` 负责持续采样。
   - `traffic-agent` 每 1~5 分钟调用 `vnstat --json`，提取指标并上报。
2. **中心节点 API**
   - 验签、幂等、去重（防止重复提交）。
   - 入库后触发账期引擎更新“本账期累计量”。
3. **前端展示层**
   - 实时总览、节点排行、单机详情、账期进度条、异常告警。

---

## 3. 数据流与上报协议

## 3.1 采集频率建议
- 小集群（<100 台）：每 60 秒上报一次。
- 中集群（100~1000 台）：每 120~300 秒上报一次。
- 大集群：可用消息队列（NATS/Kafka）削峰。

## 3.2 建议上报内容（JSON）
```json
{
  "node_id": "vps-tokyo-01",
  "hostname": "jp01",
  "timestamp": "2026-04-29T12:30:00Z",
  "iface": "eth0",
  "counters": {
    "rx_total_bytes": 96318236612,
    "tx_total_bytes": 38266192391,
    "rx_today_bytes": 3812821132,
    "tx_today_bytes": 2188371120,
    "rx_month_bytes": 521338188112,
    "tx_month_bytes": 309188112333
  },
  "hourly": [
    {"time":"2026-04-29T11:00:00Z","rx":172312333,"tx":90123123},
    {"time":"2026-04-29T12:00:00Z","rx":189331002,"tx":95311001}
  ],
  "daily": [
    {"date":"2026-04-29","rx":3812821132,"tx":2188371120},
    {"date":"2026-04-28","rx":4499921132,"tx":2499031120}
  ],
  "agent_version": "1.0.0",
  "nonce": "d5d6f..."
}
```

## 3.3 安全建议
- **HTTPS 必须开启**。
- 优先 `mTLS`（每节点发客户端证书）。
- 或 `HMAC-SHA256`：`X-Signature` + `X-Timestamp` + `X-Nonce`。
- API 端做：
  - 时间窗校验（例如 5 分钟内有效）
  - nonce 防重放
  - 节点级 QPS 限速

---

## 4. 数据库设计（PostgreSQL）

## 4.1 核心表
1. `nodes`
   - `id, node_id, provider, region, plan_gb, billing_day, timezone, status, tags`
2. `traffic_snapshots`
   - 每次上报快照：`node_id, ts, rx_total, tx_total, rx_today, tx_today, rx_month, tx_month`
3. `traffic_hourly`
   - `node_id, hour_ts, rx_bytes, tx_bytes`
4. `traffic_daily`
   - `node_id, day, rx_bytes, tx_bytes`
5. `billing_cycles`
   - `node_id, cycle_start, cycle_end, quota_bytes, reset_strategy`
6. `billing_usage`
   - **逻辑账期累计**：`node_id, cycle_start, used_rx, used_tx, used_total, updated_at`
7. `alerts`
   - `node_id, level, type, message, created_at, resolved_at`

## 4.2 为什么用“逻辑重置”
不建议定期清空 `vnstat` 原始计数器。更稳妥方式：
- 保留全量历史。
- 在账期起点记录基线（baseline）。
- 当前账期用量 = 当前累计 - baseline。
- 优点：可追溯、可补算、避免误删历史。

---

## 5. 账期自动重置设计

## 5.1 规则
- 每台 VPS 独立配置：
  - `billing_day`（1~28/31）
  - `timezone`（例如 `Asia/Shanghai`）
  - `quota_gb`（套餐流量）
- 在本地时区到达账期起点时，创建新 cycle，并设置 baseline。

## 5.2 计算公式
- `used_rx = now_rx_total - baseline_rx_total`
- `used_tx = now_tx_total - baseline_tx_total`
- `used_total = used_rx + used_tx`
- `usage_percent = used_total / quota_bytes * 100`

## 5.3 边界处理
- 节点重装导致计数器回退：检测 `now_total < previous_total`，自动重建 baseline 并打标事件。
- 机器离线补报：按 `timestamp` 回填 hourly/daily。
- 时区切换：下个账期生效，避免当前账期拆分。

---

## 6. 页面设计（炫酷+直观）

## 6.1 页面结构
1. **总览大盘（NOC）**
   - 在线节点数 / 离线节点数
   - 总 RX/TX（今日、账期、本月）
   - 账期使用率 Top10（进度条+阈值色）
2. **节点列表**
   - 支持筛选：区域、运营商、标签、状态、账期超限
   - 卡片显示：当前速率、今日用量、账期用量、剩余额度
3. **节点详情页**（参考你的截图风格）
   - 24h RX/TX 横向条形图（青+粉）
   - 30 天日流量表格
   - 账期进度环 + 预测何时跑满
4. **告警中心**
   - 超额（>90% / >100%）
   - 节点失联（5 分钟无上报）
   - 流量突增（同比小时均值）

## 6.2 UI 风格建议（赛博朋克）
- 背景：深色网格 + 轻微噪点。
- 主色：`#00F7FF`（RX）、`#FF006E`（TX）、`#39FF14`（边框/高亮）。
- 字体：像素风标题 + 等宽正文字体。
- 动效：
  - 卡片呼吸发光（低频）
  - 数据变化数字翻牌
  - 超限时边框脉冲动画
- 技术栈：`React + Tailwind + ECharts` 或 `Vue + NaiveUI + ECharts`。

---

## 7. 通信方式推荐

## 7.1 上报通道
- **首选：HTTPS Pull-less Push**（Agent 主动 POST）
  - 实施简单，易穿透 NAT。
  - 中心端统一鉴权与审计。

## 7.2 实时展示通道
- Web 端与中心端使用 **WebSocket/SSE**：
  - SSE 更简单，适合单向推送。
  - WebSocket 适合后续做远程命令等双向能力。

## 7.3 可选增强
- 大规模场景引入 `NATS/Kafka`：
  - Agent -> Ingest Gateway -> MQ -> Consumer -> DB
  - 防抖、削峰、重试更优雅。

---

## 8. MVP 落地步骤（两周）

### 第 1 周
1. VPS 安装 `vnstat + agent`（systemd 守护）。
2. 中心 API 完成 `/ingest`、验签、入库。
3. 完成 nodes + snapshots + billing_usage 表。

### 第 2 周
1. 完成 Dashboard（总览 + 节点详情）。
2. 实现账期引擎（自动切期、基线计算）。
3. 告警（Telegram/Email/Webhook）。

---

## 9. 运维与可靠性
- API/DB 全链路监控：Prometheus + Grafana。
- 每日备份 PostgreSQL（保留 7~30 天）。
- Ingest API 开启幂等键（`node_id + timestamp + nonce`）。
- Agent 端离线缓存（本地队列）避免短时网络抖动丢数据。

---

## 10. 你可以直接照着做的技术选型
- **Agent**：Bash/Python（读取 `vnstat --json`）
- **中心后端**：Go（Gin/Fiber）或 Node.js（Fastify）
- **数据库**：PostgreSQL + Redis
- **前端**：React + ECharts + Tailwind
- **部署**：Docker Compose（MVP）→ K8s（规模化）

如果你愿意，我下一步可以直接给你：
1) `agent` 的可运行脚本（systemd + 重试 + 签名）；
2) 中心 API 的 `OpenAPI` 文档；
3) 前端首页线框图与配色 CSS 变量（和你图里同款风格）。
