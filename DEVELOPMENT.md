# MT5 Hedge 开发文档

本文档描述当前代码版本的系统边界、模块结构和开发约定。历史设计草案已收敛到当前实现口径；更细的策略、风控、部署和 API 细节分别见 `docs/strategy.md`、`docs/risk.md`、`docs/deployment.md`、`docs/api.md`。

## 1. 当前定位

系统当前定位是：

- 实时行情采集与跨平台价差扫描。
- 价差、资金费、报价时差等策略研究。
- Paper 自动交易验证。
- 受保护的 MT5 demo/live 执行接入。
- Hyperliquid live 账户、仓位、订单快照只读查询。
- Hyperliquid paper 本地撮合；可选 paper-live 最小真实探针单取成交价。

当前不是全自动实盘套利系统。Hyperliquid `execution_mode=live` 下单仍被 readiness 阻止；任何实盘相关动作必须通过系统开关、平台开关、readiness 和对冲组生命周期保护。

## 2. 技术栈

- 后端：FastAPI、SQLAlchemy、PostgreSQL、APScheduler、PyJWT。
- 前端：Vite、React、TypeScript、Ant Design、TanStack Query、ECharts。
- 行情：Hyperliquid 原生 WebSocket/HTTP `l2Book`，MT5 Python API 高频 tick 轮询。
- 数据库：当前推荐 PostgreSQL；旧 SQLite 可按 `docs/deployment.md` 迁移。
- 实时推送：页面级 SSE，使用 `Authorization: Bearer <token>` 请求头鉴权。

## 3. 目录结构

```text
backend/
  app/
    accounts/        # 账户快照同步
    adapters/        # Hyperliquid、MT5、Paper 适配层
    analytics/       # 价差、资金费、报价时差研究
    api/             # FastAPI 路由和 SSE 快照
    auth/            # 登录、JWT、权限依赖
    config/          # 环境变量和运行配置
    db/              # ORM 模型、初始化、保留策略
    diagnostics/     # 链路监控诊断聚合
    execution/       # 执行网关、对冲池、回查、自动平仓
    market/          # 行情缓存、扫描器、品种映射、MT5 会话
    risk/            # 风控预检查
    strategy/        # 成本、统计信号、价差口径
    workers/         # 调度器和行情 worker
  tests/

frontend/
  src/
    api/             # Axios client
    components/      # 通用表格、状态灯、单元格截断等
    hooks/           # 页面级 SSE hook
    layouts/         # 应用外壳和页头
    pages/           # 各业务页面
    utils/           # 格式化、表格滚动判断等

docs/
  README.md          # 文档入口
  api.md             # HTTP API 和 SSE
  deployment.md      # 启动、环境变量、迁移、实盘注意
  evolution-roadmap.md
  risk.md
  strategy.md
```

## 4. 核心运行链路

### 4.1 行情

行情 worker 持续更新内存 `QuoteCache`：

- Hyperliquid：live 模式下使用 WS `l2Book`，执行前可主动 HTTP L2 复核。
- MT5：通过本机 MetaTrader5 终端轮询 `symbol_info_tick()`。
- 扫描器不直接请求网络报价，只读取同步后的报价对。

报价必须带有本地接收时间，扫描使用宽松同步窗口，执行前使用严格同步窗口。执行前缓存不同步时，会主动刷新 HL HTTP L2 和 MT5 tick；刷新后仍不同步则拒绝下单。

### 4.2 扫描与候选池

扫描器按当前启用品种映射运行：

1. 读取同步报价对。
2. 检查 MT5 会话、交易能力缓存和报价新鲜度。
3. 计算双方向入场价差、平仓价差、mid 价差和成本。
4. 应用统计入场线、品种硬阈值、成本保护线和利润条件。
5. 更新内存当前价差、候选机会和链路诊断状态。
6. 后台持久化任务低频同步当前表和历史桶。

价差研究使用历史快照或聚合桶；实时链路页面优先读取内存状态。

### 4.3 执行

执行入口统一走 `ExecutionGateway`：

- `dry_run`：只记录流程，不提交真实订单。
- `paper`：Hyperliquid 本地 bid/ask 撮合；MT5 可走 demo 下单。
- `paper-live` 可选：Hyperliquid 提交最小真实探针单取成交价，但对冲组仍是 paper 账本。
- `live`：MT5 真实下单受多重开关保护；Hyperliquid live 下单当前固定 block。

执行前会重新执行风控和严格行情复核。提交后由 `execution_reconciler` 回查 pending 订单、补腿、撤销异常 pending、刷新 live positions，并处理 closed 残仓和外部孤儿仓位。

### 4.4 对冲池

运行期使用内存 `HedgePoolStore` 管理活跃对冲组。服务启动时从数据库恢复 `opening/open/open_partial/closing/manual_intervention` 状态；自动平仓和手动平仓都先对内存状态做 CAS，避免重复触发。数据库仍是恢复、历史和审计源。

### 4.5 前端实时数据

以下页面使用页面级 SSE：

- 仪表盘：`channel=dashboard`
- 链路监控：`channel=pipeline`
- 对冲组：`channel=hedge-groups`
- 执行记录：`channel=execution`
- 仓位：`channel=positions`
- 账户：`channel=accounts`
- 风控：`channel=risk`
- 日志：`channel=logs`
- 报价时差：`channel=lead-lag`

价差研究和资金费研究当前按筛选条件直接请求分析接口，不使用 SSE。

## 5. 数据口径

- 资金和风险按 USD 名义价值汇总。
- MT5 手数会通过 `trade_contract_size`、`volume_min`、`volume_step` 换算到内部基础币数量。
- Hyperliquid 最小名义额会按当前 mid 折算为基础币数量。
- 对冲组保存触发价差、触发盘口、真实开仓价差、入场线、退出线、手续费、资金费、隔夜费和 close reason。
- `gross_spread` 是旧 API 兼容字段，等同当前 `entry_spread`。

## 6. 安全与权限

- 除登录外，API 都需要 JWT。
- 页面级 SSE 通过请求头鉴权，不允许 token 出现在 URL query。
- 管理操作使用 `require_admin`。
- 实盘开关必须输入 `ENABLE LIVE TRADING`。
- 生产或实盘相关开关启用时，默认 `JWT_SECRET` 和默认管理员密码会阻止服务启动。
- 前端不展示密钥明文。

## 7. 前端约定

- 页面顶部由 `AppLayout` 统一管理；页面级连接状态通过页头呼吸灯展示。
- 表格型页面卡片尽量占满剩余视口高度。
- 表格只有在数据量超过当前阈值时才启用内部纵向滚动，少量数据不显示无意义滚动条。
- 长文本使用 `EllipsisCell` 截断并保留 hover 可读性。
- 图表页面必须处理空数据和接口异常状态。

## 8. 开发与验证

常用命令：

```powershell
.\create_env.cmd
.\install_packages.cmd
.\start_project.cmd
```

前端构建：

```powershell
cd frontend
npm run build
```

后端测试：

```powershell
cd backend
pytest
```

修改功能时需要同步对应文档：

- API 或 SSE 变更：更新 `docs/api.md`。
- 策略、扫描、成本、信号口径变更：更新 `docs/strategy.md`。
- 风控、readiness、实盘保护变更：更新 `docs/risk.md` 和必要的 `docs/deployment.md`。
- 启动、环境变量、迁移流程变更：更新 `docs/deployment.md`。
- 阶段边界或优先级变更：更新 `docs/evolution-roadmap.md`。

## 9. 当前已知边界

- Hyperliquid live 下单未开放。
- live 自动平仓默认关闭，需要多重开关同时开启。
- MT5 会话精确度依赖本机 MetaTrader5 Python 包和券商品种能力；缺少 session API 时会退化到 tick/trade_mode 判断。
- Paper 模式默认不按真实账户可用保证金阻断；可通过策略设置开启 `paper_use_live_account_risk`。
- PostgreSQL 是当前推荐数据库，SQLite 仅作为旧版本迁移来源或轻量本地场景。

