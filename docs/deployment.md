# 部署与启动

首版目标环境为 Windows 本机，便于连接 MT5 桌面终端。

后端尽量使用当前机器已有依赖。认证 token、`.env` 读取和后台扫描调度使用标准库实现，避免 Windows 本机因 PyPI SSL 问题导致无法启动。

Alembic 模板已经保留在 `backend/alembic/`。首版启动使用 SQLAlchemy 自动建表；如果后续需要正式迁移命令，再额外安装 Alembic。

系统演进路线、PostgreSQL 迁移和实盘接入计划见 `docs/evolution-roadmap.md`。

`execution_mode=paper` 表示纸面账本执行：默认 Hyperliquid 腿使用本地 `QuoteCache` 最新 bid/ask 撮合，MT5 腿使用当前 MT5 demo 账户下单。若开启 `HYPERLIQUID_PAPER_LIVE_ORDER_ENABLED=true`，Hyperliquid 腿会改为向真实账户提交最小可成交量探针单，只把真实成交均价写入 paper 账本，paper 数量仍按策略目标数量记录。开启前需要 `MT5_DEMO_ORDER_ENABLED=true`，并确保 MT5 `account_info().trade_mode` 是 demo。`MT5_LOGIN`、`MT5_PASSWORD`、`MT5_SERVER` 仍是唯一的 MT5 登录配置；如果配置了 `MT5_LOGIN` 或 `MT5_SERVER`，paper demo 下单前会要求当前账户 login/server 与它们一致，防止终端切到其他账户后误发单。

## 后端启动

推荐直接使用 Windows 脚本：

```powershell
.\create_env.cmd
.\install_packages.cmd
.\start_project.cmd
```

`start_project.cmd` 是开发模式，会启动后端 `8000` 和 Vite 前端 `5173`。

如果使用 Nginx 绑定公网域名，推荐使用生产模式：

```powershell
.\create_env.cmd
.\install_packages.cmd
.\build_frontend.cmd
.\start_backend.cmd
```

此时前端不再运行 `5173`，而是由 Nginx 直接托管 `frontend/dist`；后端仍只监听本机 `127.0.0.1:8000`。

停止项目：

```powershell
.\stop_project.cmd
```

前端生产构建：

```powershell
.\build_frontend.cmd
```

如果需要手工启动，命令如下：

```powershell
cd C:\Users\a1998\Documents\MT5-hedge
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r backend\requirements.txt
cd backend
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

首次启动会自动：

- 按 `.env` 的 `DATABASE_URL` 创建数据库表；当前推荐 PostgreSQL。
- 初始化管理员账号。
- 首次初始化时从 `config/symbol_mappings.yaml` 导入品种映射种子。
- 执行一次价差扫描。
- 启动后台扫描任务。

## 前端启动

```powershell
cd C:\Users\a1998\Documents\MT5-hedge\frontend
npm install
npm run dev
```

打开 `http://127.0.0.1:5173`。

## 环境变量

复制 `.env.example` 为 `.env` 后修改：

```text
JWT_SECRET=change-me-before-live
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin123
DEFAULT_EXECUTION_MODE=paper
LIVE_TRADING_ENABLED=false
SCANNER_INTERVAL_SECONDS=15
SCANNER_INTERVAL_MS=0
CANDIDATE_INTERVAL_SECONDS=5
SPREAD_HISTORY_INTERVAL_SECONDS=5
SPREAD_BUCKET_SECONDS=5
SIGNAL_STATS_CACHE_TTL_MS=10000
STREAM_INTERVAL_MS=1000
QUOTE_SOURCE_MODE=paper
PAPER_QUOTE_INTERVAL_MS=200
MT5_QUOTE_POLL_INTERVAL_MS=200
HYPERLIQUID_MARKET_DATA_SOURCE=native
LOOSE_QUOTE_SYNC_MS=3000
STRICT_QUOTE_SYNC_MS=500
QUOTE_STALE_MS=1500
HYPERLIQUID_INFO_URL=https://api.hyperliquid.xyz/info
HYPERLIQUID_WS_URL=wss://api.hyperliquid.xyz/ws
HYPERLIQUID_DEFAULT_TAKER_FEE_RATE=0.00045
HYPERLIQUID_DEFAULT_MAKER_FEE_RATE=0.00015
HYPERLIQUID_DEFAULT_MIN_NOTIONAL=10
HYPERLIQUID_FEE_ROUND_TRIPS=2
HYPERLIQUID_ACCOUNT_ADDRESS=
HYPERLIQUID_SECRET_KEY=
HYPERLIQUID_PAPER_LIVE_ORDER_ENABLED=false
PAPER_LIVE_PARALLEL_EXECUTION=true
HYPERLIQUID_PAPER_LIVE_SLIPPAGE=0.01
MT5_DEFAULT_COMMISSION_RATE=0
MT5_SPREAD_REBATE_RATE=0.20
MT5_SWAP_FREE=true
DEFAULT_SLIPPAGE_BPS=0
DEFAULT_FX_COST_RATE=0
FX_FALLBACK_RATES={"JPY":0.00625}
COST_CACHE_TTL_SECONDS=60
CARRY_COST_SYNC_INTERVAL_SECONDS=300
MT5_LOGIN=
MT5_PASSWORD=
MT5_SERVER=
```

生产或实盘前必须修改 `JWT_SECRET` 和管理员密码。

Hyperliquid 测试网需要同时切换：

```text
HYPERLIQUID_INFO_URL=https://api.hyperliquid-testnet.xyz/info
HYPERLIQUID_WS_URL=wss://api.hyperliquid-testnet.xyz/ws
```

`HYPERLIQUID_ACCOUNT_ADDRESS` 填有余额的主账户地址；余额、手续费、仓位和订单快照等只读查询会使用该地址。execution reconciler 会读取账户级 `openOrders` / `userFills` 快照，用于恢复 pending 订单和唯一匹配的缺失外部订单号。因此使用 API wallet/agent 场景时，必须配置实际账户地址，而不是只配置 agent wallet 地址。`HYPERLIQUID_SECRET_KEY` 只用于 `HYPERLIQUID_PAPER_LIVE_ORDER_ENABLED=true` 时的 paper-live 探针下单，应填写已授权的 API wallet/agent 私钥，不要填写未隔离的大额主钱包私钥。

Paper-live 探针模式只改变 Hyperliquid 成交价来源，不把策略切成 `live`：对冲组仍保存为 `execution_mode=paper`，MT5 仍走 demo 下单，PnL 和持仓数量按 paper 账本目标数量计算。Hyperliquid 真实账户会留下最小探针仓位，开仓探针使用同方向最小量，平仓探针使用 reduce-only 最小量，目的是用交易所真实回报替代本地模拟成交价。探针数量按该资产 `szDecimals` 的最小步进和 `HYPERLIQUID_DEFAULT_MIN_NOTIONAL` 折算后的最小名义金额取较大值。`PAPER_LIVE_PARALLEL_EXECUTION=true` 时，严格行情复核通过后会同时提交 HL 探针和 MT5 demo 单；若只有一边成交，系统会立即提交反向冲销单。

HIP-3 DEX 仓位不会出现在默认 `clearinghouseState` 响应里。系统会从启用的品种映射中提取 `xyz:*` 这类 DEX 前缀，并额外用 `dex=xyz` 查询账户仓位，所以仓位页可以展示 `xyz:JP225` 这类主网 DEX 仓位。

## 品种映射

系统运行时读取数据库 `symbol_mappings` 表。前端“设置 / 品种映射”里的新增、编辑、删除都会持久化到数据库，重启后不会被配置文件覆盖。

`config/symbol_mappings.yaml` 只作为首次建库时的种子文件，适合预置初始交易对：

```yaml
symbols:
  - symbol: BTC
    hyperliquid_symbol: BTC
    mt5_symbol: BTCUSD
    min_entry_spread: 0
    max_close_spread: 0
    enabled: true
```

系统只扫描数据库中启用的映射品种。`min_entry_spread` 是该品种的最小买入/入场价差，`max_close_spread` 是最大卖出/平仓价差，默认 `0` 表示不启用品种硬阈值；启用后系统先把品种硬阈值并入最终入场线/平仓线，再按统计分位数和利润条件判断。数据库已经初始化后，再修改 YAML 不会自动覆盖现有映射；需要变更时请使用前端设置页或对应 API。

## 实时行情

默认 `QUOTE_SOURCE_MODE=paper`，会启动 Paper 行情 worker，便于本地演示。

切换到 `QUOTE_SOURCE_MODE=live` 后：

- Hyperliquid worker 连接 `HYPERLIQUID_WS_URL` 并订阅每个映射品种的 `l2Book`；`HYPERLIQUID_L2BOOK_FAST_ENABLED=true` 时订阅会携带 `fast: true`，使用浅层高频盘口。
- `xyz:*` 这类 HIP-3 DEX 品种也通过原生 Hyperliquid `l2Book` WS 订阅维护报价。
- 运行期不再启动 Hyperliquid HTTP 行情轮询，避免频繁请求触发 429；HTTP `l2Book` 只在下单前主动复核时调用一次。
- 如果当前网络无法建立 Hyperliquid WebSocket，系统不会用 HTTP 后台轮询替代，需先恢复 WS 行情再执行。
- MT5 worker 初始化本机 MetaTrader5 终端，并对映射品种调用 `symbol_select(symbol, True)` 和 `symbol_info_tick()` 高频轮询。
- 扫描器只读取同步后的行情缓存，不直接请求报价。
- 非 USD 计价 MT5 品种会优先通过 MT5 实时汇率品种换算到 USD，例如 `JPY` 使用 `USDJPY`；如果实时汇率不可用，会使用 `FX_FALLBACK_RATES` 兜底。自动执行前应确认相关汇率品种在 MT5 中可见。

MT5 Depth of Market 可通过 `market_book_add()` 订阅、`market_book_get()` 读取、`market_book_release()` 释放；是否有深度数据取决于券商和品种。首版实盘判断仍以 tick bid/ask 为主，盘口深度后续按券商支持情况接入。

## 日志口径

系统有两类日志：

- 数据库业务日志：`SystemLog` / `WorkerRun`，用于前端“日志”页面查看业务事件。
- 进程日志文件：例如 `uvicorn-8001.out.log`、`uvicorn-8001.err.log`，来自 uvicorn/stdout/stderr 和 loguru。

高频扫描成功不再写入数据库日志，避免 100ms 扫描时产生大量低信息记录。数据库只保留扫描失败、自动执行成功/失败、风控和人工操作等有业务意义的日志，并自动保留最近 1000 条。

## 实盘注意

代码保留实盘执行路径和开关。Hyperliquid `execution_mode=live` 当前仍不启用全量真实下单；但 `execution_mode=paper` 可通过 `HYPERLIQUID_PAPER_LIVE_ORDER_ENABLED=true` 使用最小真实探针单取得成交价。MT5 可通过 `MT5Adapter` 受保护提交 demo 或 live market 订单，默认不会发单。接实盘前仍需要用小额账户验证券商费用模型、成交回报字段和异常恢复流程。

手工关闭 live 对冲组不再只修改数据库状态。系统先提交 Hyperliquid reduce-only 反向订单；只有该腿实际 `filled/partially_filled` 且成交数量大于 0 时，才按成交比例提交 MT5 reduce-only 平仓腿。MT5 hedging 账户平仓时会查找对应方向的 `positions_get()` 持仓 ticket，并在 `order_send` 请求中带 `position` 字段；找不到可减仓持仓或请求数量超过当前持仓时拒绝发单，避免反向单开出新仓。Hyperliquid 外部订单仅 `accepted/submitted` 时保持 `closing`，由 reconciler 后续回查后继续补 MT5 腿或升级异常。

启动和周期调度会运行 `execution_reconciler`，用于回查 `opening` / `closing` 对冲组的 pending 订单。若只有 Hyperliquid 腿存在且后续确认成交，它会按成交比例提交 MT5 开仓或 reduce-only 平仓补腿；双腿确认后再推进到 `open` / `closed`。它会刷新 live positions，其中 Hyperliquid 从 `clearinghouseState` 读取 perp 仓位，MT5 从终端读取 `positions_get()`；若已标记 `closed` 的 live 对冲组仍有对应符号持仓，会把对冲组拉回 `manual_intervention` 并发出告警。若账户中存在无法匹配任何 live 对冲组的 Hyperliquid/MT5 仓位，会生成“外部孤儿仓位”告警，提示该仓位不在系统对冲组生命周期内。Hyperliquid 侧会结合 `orderStatus`、账户级 `openOrders/userFills` 恢复 pending 订单。

管理员也可以在对冲组页面点击“同步执行状态”，或调用 `POST /api/execution/reconcile` 立即运行一次 execution reconciler。该操作只做同步、回查、撤 pending 和按配置补偿，不绕过既有 live 发单开关。

对于已经在外部账户存在、但系统没有对应 live 对冲组的仓位，可以在仓位页点击“接管”，或调用 `POST /api/positions/{id}/adopt`。该操作不会下新单，只会基于当前 `positions` 记录创建 `live/manual_intervention` 对冲组和接管事件。导入的单腿组后续手工平仓时只关闭非零数量的平台腿，并按 reduce-only 规则减掉已有持仓，避免为不存在的另一边补发订单。

前端“执行记录”页面用于查看订单和成交。订单列表会展示 `post_only`、`reduce_only`、TTL、外部单号和错误信息，便于实盘排查时确认某条平仓或补偿单是否按 reduce-only 提交。

单腿成交时默认仍是保守撤单并进入 `manual_intervention`。如果品种映射的 `single_leg_action` 设置为 `auto_close` 或 `reverse_filled_leg`，系统会尝试反向冲销已成交腿：开仓异常冲销成功后对冲组标记为 `failed`，平仓异常冲销成功后对冲组回到 `open`；补偿单没有确认成交时仍进入 `manual_intervention`。

如果 adapter 持续返回 `not_ready/not_supported`，系统会在 `EXECUTION_RECONCILE_PENDING_STALE_SECONDS` 后尝试撤销该 pending 订单，并把对冲组升级为 `manual_intervention`，避免外部订单状态不可重建时无限挂起。

live 自动平仓默认关闭。只有策略配置 `auto_close_enabled=true`、`auto_close_live_enabled=true`、系统 `live_trading_enabled=true`，并且 Hyperliquid/MT5 各自实盘发单开关也打开时，后台才会对 live 对冲组提交自动平仓腿。

实盘前可以在设置页查看“实盘就绪检查”，或调用 `GET /api/settings/live-readiness`。检查项会覆盖系统实盘总开关、Hyperliquid 账户地址和 `clearinghouseState` 只读探测、MT5 Python 包和 `account_info()` 只读探测、MT5 实盘下单开关、MT5 登录配置、启用品种映射、已同步 live 仓位归属和单腿自动补偿配置。当前 Hyperliquid `execution_mode=live` 下单项固定为 block；paper-live 探针属于 paper readiness，不通过 live readiness 放行。外部仓位必须同时匹配 live 对冲组的平台、映射品种、方向和该平台预期数量；同品种但方向或数量不一致也会被视为未归属。若当前 `positions` 表里还有未归属 live 对冲组的外部仓位，或已关闭 live 对冲组仍有残余仓位，readiness 会直接 `block`，要求先通过 reconciler 或人工处理清理。`status=blocked` 表示不应发实盘单；`status=warning` 表示可继续小额验证但仍有需要确认的配置。live 开仓和平仓入口会执行同一套 readiness 检查，存在 `block` 项时不会提交任何实盘腿。

Paper 完整模拟前可调用 `GET /api/settings/paper-readiness`。该检查不会发单，只验证本地 Hyperliquid paper 撮合前提、MT5 demo 下单开关、当前账户 demo 状态、`MT5_LOGIN`/`MT5_SERVER` 账户锁定、MT5 登录配置和启用品种映射。paper 开仓和平仓入口会执行同一套检查，存在 `block` 项时不会提交模拟订单。

当前系统更准确的定位是“实时行情 + 策略研究 + Paper 自动交易验证 + 受保护单腿实盘执行接入”。即使设置 `execution_mode=live`，也必须同时开启系统实盘确认和对应平台发单开关。

## SQLite 迁移到 PostgreSQL

当前默认数据库为 PostgreSQL：

```text
DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/mt5_hedge
```

服务器从旧 SQLite 版本升级到 PostgreSQL 版本时，不要直接启动新后端。正确顺序是：

```powershell
cd C:\Users\Administrator\Documents\MT5-hedge
.\scripts\stop_project.ps1
git pull origin master
.\scripts\install_packages.ps1
```

确认 `.env` 已切到 PostgreSQL，并且 PostgreSQL 服务或容器已经启动后，先做一次 dry-run：

```powershell
.\scripts\migrate_sqlite_to_postgres.ps1 -DryRun
```

dry-run 会读取默认源库 `data\mt5_hedge.db`，打印各表行数，不会修改 PostgreSQL。确认行数正常后执行正式迁移：

```powershell
.\scripts\migrate_sqlite_to_postgres.ps1 -Replace -Yes
```

`-Replace` 会清空 PostgreSQL 当前业务表并导入 SQLite 全量数据；脚本会先把 SQLite 源库复制到 `data\migration-backups\时间戳\sqlite-before-postgres-migration.db`。迁移完成后会自动修复 PostgreSQL 自增序列，避免新订单、新对冲组 ID 冲突。

如果服务器旧 SQLite 不在默认位置，可以指定源库：

```powershell
.\scripts\migrate_sqlite_to_postgres.ps1 -Source C:\path\to\mt5_hedge.db -Replace -Yes
```

如果 `.env` 暂时不能改，也可以显式指定 PostgreSQL 连接串：

```text
.\scripts\migrate_sqlite_to_postgres.ps1 -TargetUrl "postgresql+psycopg2://postgres:postgres@localhost:5432/mt5_hedge" -Replace -Yes
```

迁移后再启动后端：

```powershell
.\scripts\start_backend.ps1
```

或者开发模式：

```powershell
.\scripts\start_project.ps1
```

注意事项：

- 迁移前必须停止旧后端，避免 SQLite 迁移过程中仍有写入。
- 迁移脚本默认不会覆盖已有 PostgreSQL 数据；只有传入 `-Replace` 才会清空并重导。
- 如果 PostgreSQL 已经有新版本运行产生的数据，正式执行 `-Replace` 前应先确认这些数据可以被 SQLite 历史库覆盖。
- SQLite 相对路径仍按项目根目录解析；旧库通常是 `data\mt5_hedge.db`，不是 `backend\data\mt5_hedge.db`。
