# 系统演进路线

本文档用于记录系统从本地 Paper / 模拟交易，逐步演进到远程数据库、稳定运行和实盘交易的路线。

## 当前状态

当前系统已经具备：

- Hyperliquid + MT5 行情接入框架。
- 品种映射数据库持久化。
- 实时价差扫描、候选机会、价差研究。
- 统计入场线 `reachable_entry` 和统计退出线 `exit_target`。
- Paper 自动开仓、Paper 自动平仓。
- 风控参数、账户读取、保证金检查框架。
- SSE 实时推送。
- SQLite 本地数据库。

当前系统还没有真正启用实盘交易能力：

- Hyperliquid live 下单适配器仍返回保护性失败。
- MT5 live 下单适配器仍返回保护性失败。
- live 模式路径存在，但真实下单、撤单、成交回报、异常补偿还没有完成。
- 自动平仓目前只支持 Paper 对冲组，不会对 live 对冲组发反向单。

因此当前更准确的定位是：

```text
实时行情 + 策略研究 + Paper 自动交易验证系统
```

还不是：

```text
可直接实盘运行的自动交易系统
```

## PostgreSQL 是否能减轻压力

可以，但要分清它减轻的是哪类压力。

远程 PostgreSQL 能改善：

- SQLite 高频写入时的锁竞争。
- 长时间运行后本地数据库膨胀导致的查询变慢。
- 前端查询、后台扫描、日志写入同时发生时的读写并发。
- 数据备份、迁移、监控和跨机器访问。
- 后续多后端实例或独立分析服务读取同一数据库。

远程 PostgreSQL 不能直接改善：

- MT5 Python API 轮询本身的 CPU/阻塞。
- Hyperliquid WebSocket 网络延迟。
- 策略计算重复执行。
- 前端图表渲染大数组。
- VPS 到数据库机器之间的网络延迟。

如果数据库部署在其他机器上，扫描器每轮写库会多一段网络往返。对于 100ms 级扫描，这意味着不能把每次扫描都设计成强依赖远程数据库写入。合理架构应该是：

```text
行情/扫描热路径：内存状态为主
当前状态：低频批量 upsert 到数据库
历史数据：聚合后写入数据库
前端展示：读 current 表和聚合表
```

所以 PostgreSQL 是中长期必要升级，但它不是替代算法优化和写入降频的手段。

## 推荐部署形态

### 阶段 1：单机 SQLite

适合当前开发和 Paper 验证。

```text
Windows VPS
  - MT5 桌面终端
  - FastAPI 后端
  - React 前端
  - SQLite
```

优点：

- 简单。
- 没有网络数据库依赖。
- MT5 终端和 Python API 在同一台 Windows 上，集成成本低。

缺点：

- SQLite 写入并发弱。
- 数据备份和迁移不方便。
- 长时间高频运行需要严格控制写入量。

### 阶段 2：单后端 + 远程 PostgreSQL

适合 Paper 长时间运行和准实盘前验证。

```text
Windows VPS
  - MT5 桌面终端
  - FastAPI 后端
  - React 前端

Database VPS / Managed PostgreSQL
  - PostgreSQL
```

建议条件：

- 数据库和 Windows VPS 在同一地域。
- 后端仍保持内存热路径，不把每个 tick 都同步写库。
- 开启连接池、statement timeout、慢查询日志。

优点：

- 数据可靠性和查询并发明显好于 SQLite。
- 便于备份、分析、迁移。
- 后续可以把研究/报表服务拆出去。

风险：

- 网络抖动会影响写库。
- 如果热路径强依赖数据库，远程 DB 反而会放大延迟。

### 阶段 3：行情/执行与 Web 服务拆分

适合实盘前。

```text
Windows VPS
  - MT5 终端
  - 行情 worker
  - 执行 worker

App/API VPS
  - FastAPI API
  - 前端

PostgreSQL
  - 业务状态
  - 历史聚合
  - 审计日志
```

这一步的关键不是拆得多，而是把职责边界拆清楚：

- 行情进程只负责可靠更新行情。
- 策略进程只负责产生信号。
- 执行进程只负责下单、撤单、补偿和状态机。
- API 只负责展示、配置和人工操作。

阶段 3 前不建议贸然多进程化，否则排错会变复杂。

## PostgreSQL 迁移路线

### 1. 准备 SQLAlchemy 兼容性

当前代码已经通过 `DATABASE_URL` 抽象数据库连接，理论上可以切换：

```text
DATABASE_URL=postgresql+psycopg://user:password@host:5432/mt5_hedge
```

但在正式切换前需要确认：

- 依赖中加入 PostgreSQL driver，例如 `psycopg`。
- SQLite 特有行为不被依赖。
- Boolean、DateTime、Text、Float 类型在 PostgreSQL 下正常建表。
- 自动建表之外，最好恢复 Alembic 迁移流程。

### 2. 引入 Alembic 正式迁移

当前启动会自动补列，适合开发，但不适合生产数据库长期演进。

迁移到 PostgreSQL 前建议：

- 固化当前模型为初始 migration。
- 后续字段变更都走 migration。
- 禁止生产环境依赖运行时 `ALTER TABLE` 自动补列。

### 3. 数据迁移

从 SQLite 到 PostgreSQL 的数据可以分层迁移：

- 必须迁移：
  - `users`
  - `strategy_settings`
  - `risk_settings`
  - `symbol_mappings`
  - `system_settings`
- 可选择迁移：
  - `hedge_groups`
  - `orders`
  - `fills`
  - `risk_events`
  - `system_logs`
- 可以丢弃或按需归档：
  - `market_snapshots`
  - `spread_snapshots`
  - 长周期 `spread_buckets`

### 4. PostgreSQL 性能设置

首版推荐先保持简单：

- 给高频查询字段建索引：
  - `spread_current.symbol`
  - `arbitrage_opportunities.status`
  - `hedge_groups.status`
  - `spread_buckets(symbol, direction, bucket_start)`
  - `system_logs.created_at`
- 历史表按时间做保留策略。
- 不要每次扫描都写大日志。
- 数据库连接池设置小一些，2 核 4G 环境不需要大池。

## 性能演进路线

优先级建议：

1. SQLite WAL / busy timeout。
2. 成本、费率、汇率、MT5 symbol_info TTL 缓存。
3. 日志和风险事件去重冷却。
4. 历史数据保留策略。
5. 扫描热路径内存化，数据库批量 flush。
6. Dirty symbol 扫描，只计算有行情变化的品种。
7. 价差研究图表刷新节流。
8. PostgreSQL 迁移。
9. 行情、策略、执行进程拆分。

前 1-4 是低风险优化，应该优先做。PostgreSQL 建议放在 8，不是因为不重要，而是因为在热路径还没降写入之前，远程数据库可能只是把 SQLite 的本地锁问题变成网络延迟问题。

## NautilusTrader 接入路线

NautilusTrader 可以作为后续执行内核候选，但不应该在当前阶段直接替换整个系统。它更适合接管订单生命周期、订单事件、成交事件、仓位组合和 Paper/Live 语义一致性；我们的系统继续负责价差研究、品种映射、成本模型、风控配置、UI 和双腿对冲组业务语义。

### 适合它负责的边界

NautilusTrader 适合负责：

- 单腿订单生命周期：提交、接受、拒绝、部分成交、完全成交、撤单、过期。
- 订单列表和高级订单关系：例如 OCO、OTO、OUO、Bracket。
- Paper / Backtest / Live 的统一订单事件语义。
- 成交事件、仓位、Portfolio 和本地 Cache。
- Hyperliquid 侧 live market data 和 execution，因为 NautilusTrader 已有官方 Hyperliquid adapter。

它不应该直接负责：

- JP225 等 MT5 合约与 Hyperliquid 数量换算。
- MT5 券商品种规格、交易时段和本机终端状态。
- 统计入场线、统计退出线和价差研究。
- 双腿对冲组是否成立、是否回滚、是否人工介入。
- 前端配置、审计、业务日志和运维界面。

### Hyperliquid 与 MT5 现实边界

Hyperliquid：

- NautilusTrader 原生支持 Hyperliquid adapter。
- 后续可以优先让 NautilusTrader 接管 Hyperliquid 单腿订单生命周期。

MT5：

- 当前未使用 NautilusTrader 官方 MT5 adapter。
- MT5 是本机桌面终端 + Python 包模式，不是标准 REST/WebSocket venue。
- 第一阶段不建议直接写完整 Nautilus MT5 adapter。
- 更稳的方式是保留现有 MT5Adapter，先做桥接层，把 MT5 订单状态和成交回报转换成统一执行事件。

### 推荐架构边界

```text
Signal Engine
  - 价差扫描
  - reachable_entry
  - exit_target
  - 成本模型
  - 风控前置判断

HedgeGroup Manager
  - 创建对冲组
  - 拆分两条腿
  - 维护双腿目标数量
  - 处理单腿成交、部分成交、失败、回滚、人工介入

Execution Gateway
  - 标准化 submit/cancel/query/reconcile 接口
  - Hyperliquid 可接 NautilusTrader
  - MT5 先接现有 MT5Adapter

NautilusTrader
  - 管单腿订单生命周期
  - 管订单/成交/仓位事件
  - 管 Hyperliquid 原生 adapter
```

核心原则：

```text
NautilusTrader 管 order/position lifecycle
我们管 hedge lifecycle
```

### 接入阶段

#### 阶段 N0：先设计兼容接口

在不引入 NautilusTrader 的情况下，先把当前执行层整理成可替换接口：

- `ExecutionIntent`
- `LegOrderIntent`
- `ExecutionGateway`
- `OrderEvent`
- `FillEvent`
- `PositionEvent`
- `HedgeGroupState`

这样后续是否接 NautilusTrader，都不会影响上层策略和前端。

#### 阶段 N1：Hyperliquid 单腿 PoC

目标：

- 只接 Hyperliquid。
- 只做单品种、小额 Paper 或测试网。
- 验证 NautilusTrader 的订单事件能映射到我们的 `orders`、`fills`、`hedge_group_events`。

不做：

- 不接 MT5。
- 不做完整双腿自动套利。
- 不直接启用 live 自动执行。

#### 阶段 N2：Execution Gateway 桥接

目标：

- Hyperliquid 由 NautilusTrader 执行。
- MT5 仍由现有 Python MT5Adapter 执行。
- 两边统一输出 `OrderEvent` / `FillEvent`。
- HedgeGroup Manager 只消费统一事件，不关心底层来自 Nautilus 还是 MT5Adapter。

重点处理：

- 部分成交。
- 订单拒绝。
- 撤单失败。
- 网络超时后的状态 reconciliation。
- 单腿成交后的补偿动作。

#### 阶段 N3：双腿对冲组接入

目标：

- 一个 `HedgeIntent` 拆成两条 `LegOrderIntent`。
- Hyperliquid 腿和 MT5 腿都通过 Execution Gateway 回报状态。
- HedgeGroup Manager 根据两边事件推进：
  - `pending_open`
  - `opening`
  - `open`
  - `open_partial`
  - `manual_intervention`
  - `closing`
  - `closed`
  - `failed`

这一步才开始让 NautilusTrader 间接参与完整套利执行。

#### 阶段 N4：正式 MT5 Nautilus adapter 评估

只有在 N2/N3 稳定后，才评估是否需要写正式 MT5 adapter。

需要评估：

- MT5 Python 包是否能稳定提供订单状态和成交回报。
- 是否能可靠处理终端断线、重连、重复订单、订单 ticket 查询。
- 是否值得用 Nautilus adapter 规范重写，而不是保留桥接层。

### 不建议现在直接做的事

- 不建议现在把策略、风控、前端全部迁到 NautilusTrader。
- 不建议先写完整 MT5 Nautilus adapter。
- 不建议在真实下单闭环完成前引入复杂引擎改造。
- 不建议用 NautilusTrader 的 OCO/Bracket 直接表达我们的双腿对冲组。OCO 是一个成交另一个取消；我们的对冲组是两条腿都要成交并保持比例。

### 接入前验收条件

进入 N1 前建议先满足：

- 当前 Paper 自动开仓和平仓连续运行稳定。
- 对冲组状态机字段和事件流整理清楚。
- `orders` / `fills` / `hedge_group_events` 能表达部分成交、拒单、撤单和单腿异常。
- live 行情 + 只读账户模式稳定。
- Hyperliquid 测试网或小额主网凭证准备好。

进入 N3 前必须满足：

- Hyperliquid 单腿事件映射稳定。
- MT5Adapter 订单状态查询和成交回报可验证。
- 单腿异常补偿策略明确。
- 手动小额实盘至少跑通一次完整开仓和平仓。

## 实盘交易演进路线

### 阶段 A：Paper 成交质量验证

目标：

- 自动开仓和平仓逻辑稳定。
- 延迟模拟能暴露短窗口机会失效问题。
- 成本模型和真实平台页面显示大致一致。
- 自动执行不会疯狂触发。
- 对冲组状态机没有明显错乱。

当前正在这个阶段。

### 阶段 B：只读实盘环境

目标：

- 使用 live 行情。
- 使用真实账户余额、保证金、费率。
- 不允许真实下单。
- 对比系统机会和平台实际可成交价格。

验收重点：

- Hyperliquid 主网/测试网账户读取稳定。
- MT5 tick、交易时段、合约规格读取稳定。
- JP225、SP500、BTC、ETH 等不同计价口径都能正确换算。
- 风控不会误判名义价值、保证金和可用资金。

### 阶段 C：最小实盘下单闭环

目标：

- 单品种、小名义金额。
- 手动点击执行，不自动执行。
- 支持真实下单、查询订单、查询成交。
- 下单失败时状态可恢复。

必须实现：

- Hyperliquid market / limit 下单。
- MT5 market 下单。
- 两边成交回报记录。
- 单腿成交异常处理。
- 撤单和订单状态查询。
- 幂等下单，避免重复发单。

这一阶段可以开始做 NautilusTrader N1/N2 PoC，但不应该让它直接控制完整双腿自动执行。

### 阶段 D：受限自动实盘

目标：

- 只允许白名单品种。
- 只允许小额。
- 只允许单个未平对冲组。
- 自动执行和自动平仓都需要严格风控。

必须增加：

- 实盘 kill switch。
- 单日最大亏损。
- 单日最大下单次数。
- 单品种冷却。
- 连续失败暂停。
- 单腿异常自动进入人工介入。
- 操作审计和告警。

这一阶段如果 NautilusTrader 的 Hyperliquid 单腿接入稳定，可以让 Execution Gateway 在 Hyperliquid 腿使用 NautilusTrader，MT5 腿继续使用桥接层。

### 阶段 E：稳定实盘

目标：

- 扩大品种和额度。
- 引入更完整的盘口深度和滑点模型。
- 独立监控和告警。
- 数据库备份和灾难恢复。

## 实盘前硬性检查清单

实盘前必须确认：

- `JWT_SECRET`、管理员密码已修改。
- `LIVE_TRADING_ENABLED` 默认仍为 false，必须人工开启。
- Hyperliquid API 权限最小化。
- MT5 账户是预期账户，不是误连其他账户。
- 所有启用品种都已同步 MT5 规格。
- 非 USD 品种汇率可实时读取或有可靠兜底。
- 单笔名义价值、最大未平组数、总敞口限制已设置。
- 自动执行默认关闭，先手动实盘验证。
- 日志、订单、成交、对冲组状态可追溯。
- 单腿异常处理已经演练。
- 平仓路径已经实盘小额验证。

## 建议近期版本目标

近期不建议直接做 PostgreSQL 和实盘下单。更稳的顺序是：

1. 完成低风险性能优化：WAL、TTL 缓存、日志冷却、历史保留。
2. 在 Paper 模式连续运行 3-7 天，观察候选、开仓、平仓、成本和延迟。
3. 切到 live 行情 + 只读账户，继续 Paper 执行，验证真实数据下策略表现。
4. 整理执行接口边界，为后续 NautilusTrader 接入保留 `ExecutionGateway` 抽象。
5. 整理 PostgreSQL migration，再考虑迁移数据库。
6. 最后接入真实下单，从单品种、小金额、手动执行开始。
