# 部署与启动

首版目标环境为 Windows 本机，便于连接 MT5 桌面终端。

后端尽量使用当前机器已有依赖。认证 token、`.env` 读取和后台扫描调度使用标准库实现，避免 Windows 本机因 PyPI SSL 问题导致无法启动。

Alembic 模板已经保留在 `backend/alembic/`。首版启动使用 SQLAlchemy 自动建表；如果后续需要正式迁移命令，再额外安装 Alembic。

系统演进路线、PostgreSQL 迁移、NautilusTrader 接入计划和实盘接入计划见 `docs/evolution-roadmap.md`。

## 后端启动

```powershell
cd C:\Users\a1998\Documents\MT5-hedge\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

首次启动会自动：

- 创建 SQLite 数据库 `data/mt5_hedge.db`。
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
STREAM_INTERVAL_MS=1000
QUOTE_SOURCE_MODE=paper
PAPER_QUOTE_INTERVAL_MS=200
MT5_QUOTE_POLL_INTERVAL_MS=200
HYPERLIQUID_HTTP_POLL_INTERVAL_MS=1000
LOOSE_QUOTE_SYNC_MS=3000
STRICT_QUOTE_SYNC_MS=500
QUOTE_STALE_MS=1500
HYPERLIQUID_INFO_URL=https://api.hyperliquid.xyz/info
HYPERLIQUID_WS_URL=wss://api.hyperliquid.xyz/ws
HYPERLIQUID_DEFAULT_TAKER_FEE_RATE=0.00045
HYPERLIQUID_DEFAULT_MAKER_FEE_RATE=0.00015
HYPERLIQUID_FEE_ROUND_TRIPS=2
MT5_DEFAULT_COMMISSION_RATE=0
MT5_SPREAD_REBATE_RATE=0.20
MT5_SWAP_FREE=true
DEFAULT_SLIPPAGE_BPS=0
DEFAULT_FX_COST_RATE=0
FX_FALLBACK_RATES={"JPY":0.00625}
COST_CACHE_TTL_SECONDS=60
HYPERLIQUID_PRIVATE_KEY=
HYPERLIQUID_WALLET_ADDRESS=
HYPERLIQUID_ACCOUNT_ADDRESS=
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

如果使用 API wallet/agent private key，`HYPERLIQUID_ACCOUNT_ADDRESS` 应填写有余额的主账户地址；余额、手续费等账户查询会优先使用该地址。

## 品种映射

系统运行时读取数据库 `symbol_mappings` 表。前端“设置 / 品种映射”里的新增、编辑、删除都会持久化到数据库，重启后不会被配置文件覆盖。

`config/symbol_mappings.yaml` 只作为首次建库时的种子文件，适合预置初始交易对：

```yaml
symbols:
  - symbol: BTC
    hyperliquid_symbol: BTC
    mt5_symbol: BTCUSD
    enabled: true
```

系统只扫描数据库中启用的映射品种。数据库已经初始化后，再修改 YAML 不会自动覆盖现有映射；需要变更时请使用前端设置页或对应 API。

## 实时行情

默认 `QUOTE_SOURCE_MODE=paper`，会启动 Paper 行情 worker，便于本地演示。

切换到 `QUOTE_SOURCE_MODE=live` 后：

- Hyperliquid worker 连接 `HYPERLIQUID_WS_URL` 并订阅每个映射品种的 `l2Book`。
- 如果当前网络无法建立 Hyperliquid WebSocket，系统会用 HTTP `l2Book` 按 `HYPERLIQUID_HTTP_POLL_INTERVAL_MS` 轮询兜底。
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

首版代码保留实盘执行路径和开关，但真实 Hyperliquid SDK 与 MT5 下单调用仍处于保护状态。接实盘前必须补齐真实下单、撤单、成交回报、异常补偿和券商费用模型。

当前系统更准确的定位是“实时行情 + 策略研究 + Paper 自动交易验证系统”。即使设置 `execution_mode=live` 并开启实盘开关，Hyperliquid 与 MT5 适配器仍不会真正发出实盘订单。

## PostgreSQL 规划

当前默认数据库为 SQLite：

```text
DATABASE_URL=sqlite:///data/mt5_hedge.db
```

后续可以切换到远程 PostgreSQL，例如：

```text
DATABASE_URL=postgresql+psycopg://user:password@host:5432/mt5_hedge
```

但正式切换前需要补齐 PostgreSQL driver、Alembic 正式迁移、索引和数据迁移流程。远程 PostgreSQL 可以改善长期运行和读写并发，但不能替代扫描热路径内存化、写库降频和日志降噪。
