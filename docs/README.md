# 文档入口

本目录按“入口、实现细节、运行说明、后续路线”划分，避免同一信息在多个文件里重复维护。

## 推荐阅读顺序

1. `../DEVELOPMENT.md`：当前系统定位、模块结构、运行链路和开发约定。
2. `deployment.md`：本机启动、生产构建、环境变量、PostgreSQL 迁移和实盘注意。
3. `api.md`：HTTP API、页面级 SSE 和前端数据推送口径。
4. `strategy.md`：行情同步、扫描、成本、统计信号、自动平仓和对冲池。
5. `risk.md`：风控模式、开仓前检查、资金口径、实盘保护和 readiness。
6. `agent-design.md`：Agent 辅助诊断、建议和受控补残腿的功能设计。
7. `page-experience-improvements.md`：逐页用户体验改进清单。
8. `evolution-roadmap.md`：当前版本边界、已完成演进、下一阶段优先级和非目标。

## NautilusTrader V1 边界

- 品种映射支持 `leg_a_venue/leg_a_symbol` 与 `leg_b_venue/leg_b_symbol`，旧 HL+MT5 字段继续保留。
- Hyperliquid 和 MT5 固定使用当前项目原生实现；Binance、OKX、Bybit 等新增 venue 通过 NautilusTrader 只读 adapter 接入。Binance V1 的行情、账户、持仓均调用 NautilusTrader Binance adapter，不再使用项目内手写 Binance REST 读取。
- V1 只开放行情、账户、持仓只读能力；只有 `leg_a=hyperliquid` 且 `leg_b=mt5` 的当前原生链路会进入自动执行，其他 pair 只显示观察型行情和候选。
- API 和前端展示统一使用 mapping 的 leg metadata 渲染真实交易所名称和 symbol；方向 `long_leg_a_short_leg_b` / `long_leg_b_short_leg_a` 在页面上显示为“多对应交易所 / 空对应交易所”，不再把 Leg A/B 当成用户可见名称。
- NautilusTrader 是可选依赖，使用前单独安装 `backend/requirements-nautilus.txt`。当前固定 `nautilus-trader==1.229.0`，避免后续 Python/Rust v2 API breaking changes 影响主服务。Binance Futures 已通过 Nautilus 接入 paper-live 最小真实探针单；其他 Nautilus venue 默认只读，只有注册真实探针能力后才会发单。
- 交易所 API 密钥可在设置页“交易所配置”维护，凭证加密后保存到数据库；默认使用 `JWT_SECRET` 派生加密密钥，也可单独配置 `EXCHANGE_CONFIG_SECRET`。

## 各文档职责

- `DEVELOPMENT.md`：面向开发者的总览，只写当前版本事实和开发约定，不再放早期草案。
- `api.md`：接口契约和 SSE channel。新增、删除或改字段时优先更新这里。
- `deployment.md`：怎么启动、怎么配置、怎么迁移、实盘前要注意什么。
- `strategy.md`：策略、扫描、成本、信号和对冲池的业务口径。
- `risk.md`：风控、资金、readiness、实盘保护。
- `agent-design.md`：Agent 接入架构、工具边界、安全门槛、前端呈现和分阶段落地。
- `page-experience-improvements.md`：按页面维护体验问题、优先级和后续改进方向。
- `evolution-roadmap.md`：下一步做什么、不做什么、进入下一阶段的条件。

## 维护规则

- 功能更新必须同步对应 `.md` 文件。
- 不把同一段完整说明复制到多个文档；其他文档只引用主文档。
- 过期的“计划/建议/草案”要么移入路线图，要么删除。
- 当前代码尚未实现的能力必须明确写成“未开放”“后续计划”或“非目标”。
- 涉及实盘、凭证、风控、下单和仓位接管的变更，需要同时检查 `deployment.md`、`risk.md` 和 `api.md`。

