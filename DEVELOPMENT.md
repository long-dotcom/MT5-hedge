# Hyperliquid 与 MT5 套利系统开发文档

## 1. 项目目标

本项目目标是搭建一个用于 Hyperliquid 与 MT5 之间进行跨平台对冲套利的 Python 系统。系统需要能够自动扫描品种价差、评估真实交易成本、生成套利信号、执行双边对冲、管理资金与风险，并通过前端完整展示账户、仓位、对冲组、历史交易、日志和风控状态。

第一阶段优先实现可验证、可回放、可模拟交易的系统骨架，避免直接进入实盘交易。实盘交易必须在行情、成本、执行、风控、异常保护和权限审计全部具备后再开启。

## 2. 总体原则

- 先模拟，后实盘。
- 先保证单边风险可控，再追求价差收益。
- 所有交易动作必须可追踪、可审计、可回放。
- 所有策略参数必须可配置，不硬编码在业务逻辑中。
- 所有资金、仓位、订单、成交和对冲组状态必须以数据库为准。
- 前端只能展示脱敏后的 API 配置，禁止返回密钥明文。
- 任何异常情况下默认禁止开新仓，允许人工干预和风险平仓。

## 3. 技术栈建议

### 3.1 后端

- Python 3.11+
- FastAPI：HTTP API 与前端服务接口
- SQLAlchemy 2.x：数据库 ORM
- Alembic：数据库迁移
- PostgreSQL：生产数据库
- SQLite：本地开发可选
- Redis：任务状态、缓存、短周期行情和锁
- APScheduler 或 Celery：定时任务和后台任务
- Pydantic Settings：配置管理
- structlog 或 loguru：结构化日志

### 3.2 前端

- React + TypeScript
- Vite
- Ant Design 或 shadcn/ui
- TanStack Query：接口数据请求和缓存
- ECharts 或 Recharts：图表展示
- Zustand 或 Redux Toolkit：前端状态管理

### 3.3 交易与行情

- Hyperliquid 官方 SDK 或 HTTP/WebSocket API
- MetaTrader5 Python 包
- WebSocket 优先用于实时行情，HTTP 用于补偿和账户查询

## 4. 建议目录结构

```text
backend/
  app/
    adapters/          # Hyperliquid、MT5 适配层
    api/               # FastAPI 路由
    auth/              # 登录、权限、审计
    config/            # 配置读取、参数管理
    db/                # 数据库连接、迁移入口
    diagnostics/       # 行情链路、扫描状态和对冲池诊断聚合
    execution/         # 下单、撤单、成交确认、异常补偿
    market/            # 行情、盘口、价差扫描、品种映射
    portfolio/         # 资金、仓位、PnL 计算
    records/           # 对冲组、订单、成交、历史记录
    risk/              # 风控、杠杆、保证金、爆仓保护
    strategy/          # 信号生成、成本模型、开平仓逻辑
    workers/           # 定时任务、后台任务
    main.py
  alembic/
  tests/

frontend/
  src/
    api/
    components/
    layouts/
    pages/
    routes/
    stores/
    utils/

docs/
  api.md
  risk.md
  strategy.md
  deployment.md
```

## 5. 核心模块设计

### 5.1 平台适配层

适配层负责屏蔽 Hyperliquid 和 MT5 的接口差异，对上层提供统一能力。

必须提供：

- 获取账户资金
- 获取当前持仓
- 获取可交易品种
- 获取行情快照
- 获取盘口深度
- 下单
- 撤单
- 查询订单状态
- 查询成交记录
- 查询手续费、资金费、隔夜费等成本信息

统一接口建议：

```text
ExchangeAdapter
  get_symbols()
  get_account()
  get_positions()
  get_ticker(symbol)
  get_orderbook(symbol, depth)
  place_order(order_request)
  cancel_order(order_id)
  get_order(order_id)
  get_trades(order_id)
```

### 5.2 品种映射模块

Hyperliquid 与 MT5 的品种名称、合约单位、报价精度和最小交易量可能不同，需要维护映射关系。

映射字段：

- 内部统一 symbol
- Hyperliquid symbol
- MT5 symbol
- 是否启用
- 基础资产
- 报价资产
- 合约乘数
- 最小下单量
- 数量精度
- 价格精度
- 最小价格跳动
- 允许最大滑点
- 品种白名单/黑名单状态

### 5.3 价差扫描模块

扫描分为两层：

1. 慢速扫描：定期遍历两个平台品种池，发现潜在价差。
2. 快速监控：对进入候选池的品种提高扫描频率，持续评估净收益。

扫描计算必须包含：

- Hyperliquid 买一/卖一
- MT5 买一/卖一
- Hyperliquid 手续费
- Hyperliquid 资金费预估
- MT5 点差
- MT5 佣金
- MT5 隔夜费
- 预估滑点
- 汇率换算
- 下单数量约束
- 盘口深度是否足够

### 5.3.1 链路诊断模块

链路诊断模块用于把行情缓存、MT5 会话、扫描当前态、候选机会和对冲组生命周期聚合成前端可直接展示的结构化状态。它不参与交易决策，只服务运维监控。

第一版诊断接口为 `GET /api/diagnostics/pipeline`，页面为“链路监控”。显示口径：

- 每个启用品种一条行情管道，展示 Hyperliquid 报价年龄、MT5 报价年龄、双边同步时间差、扫描结果年龄、信号状态和候选状态。
- 阻塞点按 `hl_quote`、`mt5_quote`、`sync`、`scan`、`signal`、`candidate` 等阶段标识，前端用管道颜色和延迟 badge 展示。
- 对冲池只展示已经进入交易生命周期的活跃对冲组，按待执行、建仓中、持仓中、可平仓、平仓中、异常分组。

诊断延迟字段来自现有可靠时间戳，不代表函数级耗时；如果后续需要拆分成本计算、统计信号和执行闸门耗时，应在对应模块单独埋点，再并入诊断聚合。

候选机会只有在扣除全部成本后仍满足净利润阈值，才允许进入待执行状态。

### 5.4 成本模型

成本模型必须独立成模块，供扫描、信号、回测、前端展示共用。

成本项：

- Hyperliquid maker/taker fee
- Hyperliquid funding fee
- MT5 spread
- MT5 commission
- MT5 swap/overnight fee
- 预估滑点
- 汇率损耗
- 资金占用成本

输出指标：

- 毛价差
- 预估总成本
- 预估净利润
- 净利润率
- 预估年化收益率
- 盈亏平衡价差

### 5.5 策略信号模块

策略信号模块负责判断是否开仓、是否平仓、是否禁止交易。

开仓条件：

- 净利润大于最小阈值
- 年化收益率大于最小阈值
- 两边行情均未过期
- 两边盘口深度足够
- 两边账户保证金充足
- 当前品种未超过风险上限
- 当前系统不处于保护模式

平仓条件：

- 价差回归达到目标
- 持仓时间超过上限
- 风险指标触发
- 单边持仓异常
- 人工手动平仓
- 系统进入只平仓模式

### 5.6 执行引擎

执行引擎是高风险模块，必须明确处理失败路径。

必须支持：

- 双边下单
- 成交确认
- 部分成交处理
- 一边成交一边失败处理
- 滑点超限撤单
- 超时撤单
- 补单
- 回滚
- 人工介入标记

执行流程：

```text
发现机会
  -> 风控预检查
  -> 冻结计划资金
  -> 创建 pending_open 对冲组
  -> 双边下单
  -> 监听成交
  -> 校验成交数量和价格
  -> 更新为 open / open_partial / failed
```

### 5.7 对冲组管理

对冲组是系统的核心业务对象，表示一组跨平台套利仓位。

状态建议：

- pending_open：准备开仓
- opening：开仓执行中
- open_partial：部分开仓
- open：已完整开仓
- closing：平仓执行中
- closed：已平仓
- failed：执行失败
- manual_intervention：需要人工处理

对冲组必须记录：

- 品种
- 方向
- 两边平台订单
- 两边成交
- 两边持仓
- 开仓成本
- 当前浮盈亏
- 已实现盈亏
- 手续费
- 资金费
- 隔夜费
- 滑点
- 创建时间
- 开仓时间
- 平仓时间
- 操作来源

### 5.8 资金、仓位与杠杆管理

资金管理负责限制每次套利的规模，避免因为大波动或单边异常导致爆仓。

必须包含：

- 总资金统计
- 可用资金
- 已占用保证金
- 备用保证金
- 单策略资金上限
- 单品种资金上限
- 单平台资金上限
- 总杠杆
- 平台杠杆
- 品种杠杆
- 当前保证金率
- 预估强平价

### 5.9 风控模块

风控模块需要在开仓前、持仓中、平仓前持续执行。

核心限制：

- 单笔最大名义价值
- 单品种最大敞口
- 单平台最大敞口
- 最大未平对冲组数量
- 最大总杠杆
- 最大单日亏损
- 最大回撤
- 最低保证金率
- 最大允许滑点
- 最大行情延迟
- 最大 API 错误次数

保护模式：

- normal：允许开仓和平仓
- reduce_only：只允许平仓
- paused：暂停策略
- emergency_stop：紧急停止全部自动交易

### 5.10 日志与审计

日志分为系统日志、交易日志、风控日志和用户操作审计。

必须记录：

- 行情异常
- 扫描结果
- 信号生成
- 风控拒绝原因
- 下单请求
- 下单响应
- 成交回报
- 对冲组状态变化
- 用户登录
- 用户配置变更
- 手动平仓
- 紧急停止

日志需要支持前端分页、筛选、等级过滤和关键字搜索。

### 5.11 告警模块

告警渠道：

- 前端站内告警
- Telegram
- 邮件
- Webhook

告警场景：

- 单边持仓
- 保证金不足
- 接近强平
- API 断连
- MT5 断连
- 下单失败
- 成交异常
- 风控触发
- 系统进入保护模式

### 5.12 回测与模拟交易

实盘前必须实现模拟交易能力。

能力要求：

- dry-run 模式
- paper trading 模式
- 历史机会回放
- 历史成本复算
- 执行延迟模拟
- 滑点模拟
- 对冲组结果统计

## 6. 数据库表设计草案

### 6.1 用户与权限

- users
- roles
- user_roles
- permissions
- audit_logs

### 6.2 配置

- system_settings
- strategy_settings
- risk_settings
- exchange_credentials
- symbol_mappings

### 6.3 行情与机会

- market_snapshots
- spread_snapshots
- arbitrage_opportunities
- funding_rates
- cost_estimates

### 6.4 交易与仓位

- accounts
- account_snapshots
- positions
- orders
- fills
- hedge_groups
- hedge_group_events
- pnl_snapshots

### 6.5 日志与告警

- system_logs
- risk_events
- alerts
- worker_runs

## 7. 后端 API 草案

### 7.1 认证

```text
POST /api/auth/login
POST /api/auth/logout
GET  /api/auth/me
POST /api/auth/refresh
```

### 7.2 仪表盘

```text
GET /api/dashboard/summary
GET /api/dashboard/equity-curve
GET /api/dashboard/risk-summary
```

### 7.3 行情与机会

```text
GET /api/markets/symbols
GET /api/markets/spreads        # 每个品种最新一条实时价差快照
GET /api/opportunities          # 当前仍满足条件的候选池
GET /api/opportunities/{id}
```

### 7.4 对冲组

```text
GET  /api/hedge-groups
GET  /api/hedge-groups/{id}
POST /api/hedge-groups/{id}/close
POST /api/hedge-groups/{id}/mark-manual
```

### 7.5 账户与仓位

```text
GET /api/accounts
GET /api/accounts/snapshots
GET /api/positions
GET /api/orders
GET /api/fills
```

### 7.6 风控

```text
GET  /api/risk/status
GET  /api/risk/events
POST /api/risk/mode
POST /api/risk/emergency-stop
```

### 7.7 配置

```text
GET  /api/settings/strategy
PUT  /api/settings/strategy
GET  /api/settings/risk
PUT  /api/settings/risk
GET  /api/settings/symbol-mappings
POST /api/settings/symbol-mappings
PUT  /api/settings/symbol-mappings/{id}
```

### 7.8 日志与告警

```text
GET  /api/logs
GET  /api/alerts
POST /api/alerts/{id}/ack
```

## 8. 前端页面规划

### 8.1 登录页

- 用户名密码登录
- 登录失败提示
- 会话过期自动跳转

### 8.2 仪表盘

- 总权益
- 今日盈亏
- 已实现盈亏
- 未实现盈亏
- 当前风险状态
- 当前保护模式
- 未平对冲组数量
- 最近告警
- 权益曲线
- PnL 趋势图

### 8.3 价差扫描页

- 品种列表
- 仅展示每个品种最新一次扫描结果，不展示历史扫描记录
- 当前 Hyperliquid 价格
- 当前 MT5 价格
- 每份毛价差
- 每份预估成本
- 每份预估净利润
- 最近更新时间

表格要求：

- 长字符串截断展示
- 数字按合理小数位展示
- 支持分页
- 支持排序
- 支持品种过滤

### 8.4 候选机会页

- 当前可执行机会
- 只展示当前仍满足候选或执行条件的机会，价差回落后自动移出
- 每份毛价差、成本和净利润
- 风控检查结果
- 成本拆分
- 盘口深度
- 预计下单数量
- 可执行机会操作入口

### 8.5 对冲组页

- 当前对冲组
- 历史对冲组
- 状态流转
- 两边订单与成交
- 开仓成本
- 当前浮盈亏
- 净收益
- 手动平仓入口

### 8.6 账户页

- Hyperliquid 账户
- MT5 账户
- 总权益
- 可用资金
- 保证金
- 杠杆
- 资金利用率
- 账户快照历史

### 8.7 仓位页

- 平台
- 品种
- 方向
- 数量
- 开仓均价
- 当前价格
- 未实现盈亏
- 保证金占用
- 强平风险

### 8.8 风控中心

- 当前系统模式
- 风控指标
- 风控事件
- 单日亏损
- 回撤
- 保证金率
- API 健康状态
- 紧急停止按钮

### 8.9 日志中心

- 系统日志
- 交易日志
- 风控日志
- 用户操作审计
- 等级筛选
- 时间筛选
- 关键字搜索
- 分页展示

### 8.10 设置页

- 策略参数
- 风控参数
- 品种映射
- API 配置
- 告警配置
- 用户管理

## 9. 权限设计

角色建议：

- admin：全部权限
- trader：查看、启停策略、手动平仓、修改部分策略参数
- viewer：只读权限

关键权限：

- 查看仪表盘
- 查看账户
- 查看仓位
- 查看历史
- 修改策略配置
- 修改风控配置
- 启停机器人
- 手动平仓
- 紧急停止
- 管理用户
- 管理 API 配置

所有敏感操作必须写入审计日志。

## 10. 安全设计

- API key 和私钥必须加密存储。
- 前端永不返回 secret 明文。
- 配置导出必须自动脱敏。
- 登录接口需要限频。
- 后端接口必须校验权限。
- 实盘交易需要单独开关。
- 开启实盘前需要二次确认。
- 紧急停止不依赖策略模块，必须作为独立控制能力存在。

## 11. 任务调度设计

后台任务：

- 慢速品种扫描
- 快速候选监控
- 账户资金同步
- 持仓同步
- 订单状态同步
- 成交同步
- funding rate 更新
- MT5 连接健康检查
- Hyperliquid API 健康检查
- 风控巡检
- PnL 快照生成
- 日志清理归档

每个任务需要记录：

- 最近运行时间
- 运行耗时
- 成功/失败状态
- 错误信息
- 下一次运行时间

## 12. 实施阶段

### 阶段一：项目骨架与文档

- 初始化后端项目
- 初始化前端项目
- 建立配置系统
- 建立数据库模型草案
- 建立基础 API 结构
- 建立开发、测试、生产环境配置

### 阶段二：只读数据链路

- 接入 Hyperliquid 行情和账户读取
- 接入 MT5 行情和账户读取
- 实现品种映射
- 实现价差扫描
- 实现前端只读仪表盘

### 阶段三：成本、信号与模拟交易

- 实现成本模型
- 实现套利机会计算
- 实现 dry-run
- 实现 paper trading
- 实现对冲组模拟生命周期

### 阶段四：风控与执行引擎

- 实现风控模块
- 实现双边下单
- 实现部分成交处理
- 实现单边异常处理
- 实现人工介入流程

### 阶段五：前端完整管理台

- 完善账户、仓位、对冲组、历史、日志、风控、设置页面
- 完成权限校验
- 完成图表展示
- 完成分页、筛选、截断、小数位展示

### 阶段六：实盘保护与部署

- Docker 化
- 数据库迁移
- 告警渠道
- 健康检查
- 生产配置
- 实盘开关
- 紧急停止验证

## 13. 验收标准

### 13.1 模拟交易验收

- 能扫描两个平台品种价差。
- 能计算完整成本并展示净收益。
- 能生成模拟套利机会。
- 能创建模拟对冲组。
- 能完整记录订单、成交、PnL 和日志。
- 能展示账户、仓位、历史、日志和风控状态。

### 13.2 风控验收

- 保证金不足时拒绝开仓。
- 行情过期时拒绝开仓。
- API 异常时进入保护模式。
- 单边持仓时触发告警。
- 达到最大亏损时进入只平仓模式。
- 紧急停止后不再自动下单。

### 13.3 前端验收

- 所有核心页面可访问。
- 权限不足时不能访问敏感页面或执行敏感操作。
- 表格支持分页、筛选和排序。
- 长字符串会截断。
- 金额、价格、比例按统一格式展示。
- 图表在空数据和异常数据下都有合理状态。

### 13.4 实盘前验收

- dry-run 至少连续稳定运行 7 天。
- paper trading 至少覆盖完整开仓和平仓流程。
- 所有异常路径有日志和告警。
- 所有敏感操作有审计记录。
- 已验证单边成交处理。
- 已验证紧急停止。

## 14. 待确认事项

- MT5 具体券商、账户类型、手续费和隔夜费规则。
- Hyperliquid 使用主网还是测试环境。
- 是否只做永续合约与 MT5 CFD 的对冲。
- 初期支持哪些品种。
- 初始资金规模和最大可承受回撤。
- 是否需要 Telegram 告警。
- 前端是否需要多用户，还是仅本地管理员。
- 部署环境是 Windows 本机、Linux 服务器还是 Docker。
- 是否需要移动端适配。

## 15. 首版实际落地说明

当前首版按 Windows 本机、Python 3.10、SQLite、React + Ant Design 实现。

实际选择：

- 后端：FastAPI、SQLAlchemy、SQLite、APScheduler、PyJWT。
- 前端：Vite、React、TypeScript、Ant Design、TanStack Query、ECharts。
- 品种：运行时只扫描数据库 `symbol_mappings` 表中手动配置且启用的交易对，`config/symbol_mappings.yaml` 只作为首次初始化种子。
- 权限：单管理员账号，默认用户名密码来自 `.env`，未配置时为 `admin/admin123`。
- 告警：站内告警和系统日志。
- 执行：支持 `dry_run`、`paper`、`live` 三种模式；真实下单默认关闭。
- 执行网关：新增 `ExecutionGateway` 抽象、`AdapterExecutionGateway` 桥接层和 `build_execution_gateway()` 工厂入口，当前下单路径先产生统一 `OrderEvent` / `FillEvent`，再写入订单和成交表；NautilusTrader 已接入 Hyperliquid-only gateway，可通过 `NAUTILUS_HYPERLIQUID_ENABLED=true` 替换 Hyperliquid 单腿执行内核，并在 `NAUTILUS_HYPERLIQUID_SUBMIT_ENABLED=true` 时通过 bridge Strategy 提交 market/limit 单；MT5 腿仍保留现有 adapter。
- 执行回查：`execution_reconciler` 会周期回查 `opening` / `closing` 对冲组的 pending 订单，确认成交后补写 fill 并推进 hedge group 状态；同时刷新 live positions，对已关闭 live 对冲组做残余仓位告警。Hyperliquid 执行侧账户/仓位/订单状态查询会跟随 `NAUTILUS_HYPERLIQUID_ENVIRONMENT` 选择 mainnet/testnet info API。单腿成交且另一腿仍 pending 时，系统会尝试撤销未成交腿并进入人工处理；外部订单状态长时间不可重建时也会升级人工处理。
- 自动平仓：paper 自动平仓默认可用；live 自动平仓需要额外开启 `auto_close_live_enabled` 和系统实盘总开关，随后走同一套反向订单路径。
- 实盘保护：开启实盘必须在前端输入确认短语 `ENABLE LIVE TRADING`。
- 行情：新增实时 QuoteCache，扫描和执行只使用时间对齐后的报价对。
- Hyperliquid：live 行情支持 `native` 和 `nautilus` 两种来源；`native` 使用原生 WebSocket/HTTP `l2Book`，可通过 `HYPERLIQUID_L2BOOK_FAST_ENABLED=true` 使用 `fast: true` 浅盘口；`nautilus` 由 NautilusTrader data client 订阅和维护标准永续 L2 订单簿，并通过 `HyperliquidAllDexsAssetCtxs` custom data 接入 `xyz:*` HIP-3 DEX 行情。由于 `allDexsAssetCtxs` 是低频批量上下文，fast 开关开启时会额外为启用扫描的 `xyz:*` 品种订阅原生 `l2Book fast` 并写入同一个 QuoteCache，扫描和入库逻辑不变。
- MT5：live 行情路径使用 `symbol_info_tick()` 高频轮询；Depth of Market 可按券商支持再接 `market_book_*`。
- 成本：Hyperliquid fee/funding 和 MT5 swap 已优先读取真实数据，MT5 commission 使用配置兜底。
- 品种映射：前端支持 CRUD，并可从 MT5 `symbol_info()` 同步最小手数、合约大小、价格精度和最小跳动；最终最小量按两边约束折算为基础币数量后取最大值。
- 价差研究：新增后端统计 API 和前端“价差研究”页面；`15m/1h/4h` 优先使用原始价差快照统计，`24h/7d` 优先使用聚合桶，展示均值、标准差、Z-Score、分位数、半衰期、回归概率和降采样价差曲线。
- 实时扫描视图：价差扫描页改为每个品种最新快照，候选机会页改为当前候选池；扫描结果和候选池优先从内存扫描状态推送/读取，`spread_current` 与 `arbitrage_opportunities` 保留为执行、审计和重启兜底；历史 `spread_snapshots` 继续供价差研究统计。
- MT5 会话保护：新增 MT5 交易时段状态和动作级权限检查。扫描器会区分正常交易、只平仓、仅报价、盘尾禁开、开盘冷却和休市状态；不可开仓时不会生成候选机会。

实盘适配器保留真实交易边界和凭证检查。Hyperliquid 通过 NautilusTrader bridge Strategy 受保护提交单腿订单，MT5 通过 `MT5Adapter` 受保护提交 market 订单；默认均关闭。接入真实券商和主网前，需要继续补充具体账户规格、手续费、隔夜费、合约乘数、撤单、成交回报、启动恢复和异常补偿处理。
