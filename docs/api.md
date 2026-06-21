# API 文档

后端服务默认地址为 `http://127.0.0.1:8000`，所有业务接口以 `/api` 开头。除登录外，其余接口都需要 `Authorization: Bearer <token>`。

## 认证

- `POST /api/auth/login`：管理员登录。
- `POST /api/auth/logout`：退出登录。
- `GET /api/auth/me`：获取当前用户。

默认本地账号为 `admin/admin123`，生产环境必须通过 `.env` 修改。

## 仪表盘

- `GET /api/dashboard/summary`：总权益、PnL、风险模式、对冲组和告警摘要。
- `GET /api/dashboard/equity-curve`：账户权益曲线。
- `GET /api/dashboard/risk-summary`：风控配置和最近风控事件。

## 行情与机会

- `POST /api/markets/scan`：手动触发一次价差扫描。
- `GET /api/stream?token=<access_token>`：SSE 实时推送当前价差、候选机会、账户快照和价差研究 bucket 变更信号；当前价差和候选机会优先来自内存扫描状态，扫描尚未完成时回退数据库当前表。
- `GET /api/markets/symbols`：查看手动品种映射。
- `GET /api/markets/quotes`：查看实时行情缓存中的最新报价、来源和本地接收时间。
- `GET /api/markets/trading-sessions`：查看每个品种的 MT5 交易时段状态和动作级权限。
- `GET /api/diagnostics/pipeline`：查看“链路监控”页面使用的结构化诊断状态，包含每个启用品种的 HL/MT5 报价年龄、同步时间差、扫描状态、候选状态、主阻塞环节、`blockers` 多阻塞原因列表，以及当前活跃对冲组池和生命周期泳道计数。`metrics` 中的 `quote_sync_duration_ms`、`symbol_scan_duration_ms`、`cost_duration_ms`、`signal_duration_ms`、`candidate_sync_duration_ms`、`persist_duration_ms` 来自扫描器本轮真实计算耗时；`scan_age_ms` 仅表示结果新鲜度。
- `GET /api/markets/spreads?page=1&page_size=20`：查看每个品种最新一条实时价差快照，页面不展示历史扫描记录；展示口径使用 `gross_spread`、`unit_cost`、`unit_net_profit`。
- `GET /api/analytics/spread-summary?symbol=BTC&direction=long_mt5_short_hyperliquid&range=1h`：查看价差均值、标准差、Z-Score、分位数、半衰期和回归概率。
- `GET /api/analytics/spread-series?symbol=BTC&direction=long_mt5_short_hyperliquid&range=1h`：查看后端降采样后的价差曲线，支持 `15m/1h/4h/24h/7d`；`15m/1h/4h` 优先用原始快照统计，`24h/7d` 优先用聚合桶。
- `GET /api/analytics/funding-series?symbol=JP225&range=7d&bucket=day`：查看单个品种历史资金费率曲线和统计，后端会按品种映射自动查询 Hyperliquid/HIP-3 合约，支持 `24h/7d/30d/90d` 和 `raw/hour/day` 聚合。
- `GET /api/analytics/lead-lag?symbol=JP225&window_seconds=300&threshold_bps=3&max_lag_ms=2000`：查看最近报价领先/滞后分析，用内存报价历史判断 HL 与 MT5 谁先跳动、另一边是否跟随、滞后毫秒和滞后期间最大 mid 差。
- `GET /api/opportunities?page=1&page_size=20`：查看当前仍满足条件的候选机会；价差回落后对应机会会从当前池移除；展示口径使用 `gross_spread`、`unit_cost`、`unit_net_profit`。
- `GET /api/opportunities/{id}`：查看单个机会。
- `POST /api/opportunities/{id}/execute`：按当前执行模式创建对冲组。

## 对冲组

- `GET /api/hedge-groups?page=1&page_size=20`：分页查看对冲组。
- `GET /api/hedge-groups/{id}`：查看对冲组详情、事件和订单。
- `POST /api/hedge-groups/{id}/close`：手动平仓。
- `POST /api/hedge-groups/{id}/mark-manual`：标记为需要人工处理。
- Paper 自动平仓由后台调度器执行，不需要前端点击；对冲组会返回 `entry_spread`、`entry_threshold`、`exit_target`、`overheat_threshold` 和 `close_reason`。

## 账户、仓位、订单

- `GET /api/accounts`：同步并返回 Hyperliquid、MT5 两个平台的最新账户状态；Hyperliquid 会分开展示 Perp 权益、Spot USDC、可提取和可用保证金，读取失败时回退 Paper 账户。
- `GET /api/accounts/snapshots`：账户快照分页。
- `GET /api/positions`：当前仓位。
- `POST /api/positions/{id}/adopt`：管理员将已同步的 Hyperliquid/MT5 外部仓位接管为 `live/manual_intervention` 对冲组；用于处理 readiness 发现的孤儿仓位。请求体可传 `reason`，必要时可传内部 `symbol`。
- `POST /api/execution/reconcile`：管理员手工触发执行状态同步，立即刷新 live positions、回查 pending 订单、检查 closed 残仓和外部孤儿仓位。
- `GET /api/orders`：订单分页，包含 `post_only`、`reduce_only` 和 `ttl_seconds` 等执行语义字段，便于复核 live 平仓/补偿单是否按 reduce-only 提交。
- `GET /api/fills`：成交分页。

前端“执行记录”页面会同时展示订单与成交，订单表重点展示 `reduce_only`、`post_only`、外部单号和错误信息，用于排查 NautilusTrader Hyperliquid 与 MT5 live 执行回报。

## 风控

- `GET /api/risk/status`：当前风控参数。
- `GET /api/risk/events`：风控事件分页。
- `POST /api/risk/mode`：切换 `normal/reduce_only/paused/emergency_stop`。
- `POST /api/risk/emergency-stop`：触发紧急停止。

## 设置

- `GET/PUT /api/settings/strategy`：策略参数，包含统计入场线、Paper 自动执行和自动平仓参数；`auto_close_live_enabled=false` 时自动平仓只处理 paper 对冲组，开启后才允许 live 自动平仓继续进入实盘反向订单路径。自动平仓退出线按 `min(低分位价差, 开仓价差 - 单位成本 - 每份利润缓冲)` 计算，利润保护上限无效时返回 `0`。
- `GET/PUT /api/settings/risk`：风控参数。
- `GET/PUT /api/settings/symbol-mappings`：品种映射。
- `POST /api/settings/symbol-mappings`：新增单条品种映射。
- `PUT /api/settings/symbol-mappings/{id}`：更新单条品种映射。
- `DELETE /api/settings/symbol-mappings/{id}`：删除单条品种映射。
- `POST /api/settings/symbol-mappings/{id}/sync-broker`：从 MT5 `symbol_info()` 同步最小手数、步进、合约大小、价格精度和最小跳动。
- 品种映射包含执行策略字段：`execution_style`、`hl_open_order_type`、`hl_close_order_type`、`hl_post_only`、`hl_maker_offset_bps`、`hl_order_ttl_seconds`、`hl_unfilled_action`、`single_leg_action`。
- 品种映射包含 MT5 会话保护字段：`mt5_pre_close_no_open_minutes`、`mt5_post_open_cooldown_minutes`、`allow_hold_through_mt5_close`。
- `GET/PUT /api/settings/live-trading`：实盘开关。
- `GET /api/settings/live-readiness`：实盘执行就绪检查，返回总状态和 Hyperliquid NautilusTrader、MT5、全局实盘开关、只读账户连通性、品种映射、单腿补偿配置等检查项。

开启实盘时必须传入确认短语 `ENABLE LIVE TRADING`。

## 日志与告警

- `GET /api/logs`：系统日志分页。
- `GET /api/alerts`：站内告警分页。
- `POST /api/alerts/{id}/ack`：确认告警。
