# 架构迁移建议：本地采集 + 中心上报（成熟方案优先）

## 结论（先说答案）
你的方向是对的：**继续保持“节点本地采集 + 中心端汇总展示”**，但建议把“采集链路”从自研脚本升级为更成熟的可观测方案，中心端只做你关心的“流量业务视图页面”。

推荐优先级：

1. **方案 A（推荐）：Telegraf + InfluxDB + Grafana + 定制前端壳**
2. 方案 B：Vector/Fluent Bit + ClickHouse + Superset/自研前端
3. 方案 C：Prometheus + VictoriaMetrics + Grafana（偏监控、非账单）

---

## 为什么推荐方案 A
- **成熟稳定**：Telegraf/InfluxDB/Grafana 都是老牌组件，社区文档完善。
- **改造成本低**：你现在 Agent 已在 Python，本地替换或并行部署 Telegraf 都容易。
- **时间序列友好**：流量是典型时序数据，InfluxDB 查询与聚合直接。
- **“只做页面定制”可行**：可以先用 Grafana 跑通，后续只定制中心端页面（调用 Influx 查询 API）。

---

## 目标架构（建议）

```text
[VPS Node]
  vnstatd / node exporter / iptables counter
      ↓
  Telegraf (agent)
      ↓ HTTPS + token/mTLS
[Center]
  InfluxDB (time-series)
  Redis (在线状态/短缓存，可选)
  API Service (Node metadata + billing logic + auth)
      ↓
  Custom Dashboard (你定制的流量展示页)
  （并行可接 Grafana 做运维视图）
```

---

## 数据分层（避免后期重构）
1. **原始层 raw_metrics**：每 1~5 分钟上报一次（rx/tx counter）。
2. **聚合层 agg_hourly/agg_daily**：中心端定时任务生成小时/天粒度。
3. **账期层 billing_usage**：按节点账期计算配额使用率（逻辑重置，不清空原始数据）。

> 核心原则：**展示页面读聚合层和账期层，不直接扫原始层**。

---

## 成熟落地路径（4 个阶段）

### Phase 1：双写验证（1~3 天）
- 保留当前 `traffic_agent.py` 上报。
- 新增 Telegraf 并行上报到 InfluxDB。
- 对比同一节点 24h 数据偏差（允许 <1~2%）。

### Phase 2：中心端引入“账期引擎”（2~4 天）
- 增加节点元数据：`quota_gb / billing_day / timezone`。
- 每个账期记录 baseline，使用量 = 当前累计 - baseline。
- 输出统一接口：`/api/v1/dashboard`（总览）与 `/api/v1/nodes/{id}`（详情）。

### Phase 3：定制中心展示页（3~5 天）
- 只做你要的页面：总览、节点列表、节点详情、告警。
- UI 可继续用你现有赛博风格。
- 复杂图表可先由 ECharts 实现，减少前端成本。

### Phase 4：切换与下线（1~2 天）
- 将旧上报链路切为只读。
- 观察 3~7 天后下线旧链路。

---

## 你这个项目的最小改造点

- 节点侧：新增 Telegraf 配置（读取网络接口计数或 vnStat JSON）。
- 中心侧：新增 InfluxDB + 账期计算 worker。
- 展示侧：保留现有 Web，新增聚合 API 和新图表组件。

不建议一次性重写所有代码，建议“**并行接入 -> 数据核对 -> 切流**”。

---

## 风险与规避
- **计数器回绕/重置**：做 `counter reset` 检测，自动重建 baseline。
- **节点离线补报**：允许按 timestamp 回填，不按到达时间强绑定。
- **高并发写入**：先批量写 Influx，再异步聚合。
- **安全**：至少 token + timestamp + nonce；生产建议 mTLS。

---

## 什么时候考虑 ClickHouse 方案
满足以下任一条件再迁移：
- 节点规模 > 3,000 且保留高精度原始数据 > 6 个月；
- 需要复杂多维分析（地区/运营商/套餐/标签联合钻取）；
- 需要高并发导出报表。

否则 InfluxDB 足够且运维更省。

---

## 给你的执行建议（可直接定）
如果你希望“快、稳、改动小”，我建议现在就定：

- **采集**：Telegraf
- **存储**：InfluxDB
- **业务 API**：沿用当前 FastAPI（只新增账期与聚合接口）
- **展示**：你定制化前端页面（可并行保留 Grafana 供运维）

这条路线最符合你说的：
> “采用成熟方案，只定制中心端流量展示页面”。
