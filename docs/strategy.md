# 策略文档

首版策略以手动品种映射为核心，不做自动全市场匹配。系统运行时只扫描数据库 `symbol_mappings` 表中 `enabled: true` 的交易对；`config/symbol_mappings.yaml` 只作为首次初始化数据库的种子文件。

## 实时行情与时间对齐

行情获取和价差计算已经拆开：

- 行情 worker 持续更新内存 `QuoteCache`。
- 扫描器不直接请求平台报价，只读取 `QuoteSynchronizer` 输出的同步报价对。
- 宽松扫描使用 `loose_quote_sync_ms`，默认允许更大的时间窗口，只用于发现候选。
- 执行前复核使用 `strict_quote_sync_ms` 和 `quote_stale_ms`，时间差或新鲜度不达标时禁止下单。
- 每条报价保存 `local_recv_ts`、可用时保存 `exchange_ts`，并记录 `source` 和 `sequence` 便于排查。

默认 `quote_source_mode=paper` 时，系统用 Paper 行情 worker 模拟两个平台的实时报价。切换 `quote_source_mode=live` 后：

- Hyperliquid 使用 WebSocket 订阅 `l2Book`。
- MT5 使用 Python API 高频轮询 `symbol_info_tick()`。

MT5 的 Python API 还提供 Depth of Market 相关函数：`market_book_add()`、`market_book_get()`、`market_book_release()`。这类盘口数据依赖券商是否提供对应品种的 Market Depth；对很多外汇或 CFD 品种，可能只有 tick bid/ask，没有可用深度。

## MT5 交易时段与动作权限

MT5 品种存在交易时段、报价时段、盘尾休市、开盘跳空和券商临时只平仓等状态。系统不再把 MT5 简单视为 `open/closed`，而是计算动作级权限：

- `can_open_long`
- `can_open_short`
- `can_close_long`
- `can_close_short`
- `can_quote`

状态含义：

- `normal_trade`：正常交易，可按方向生成机会。
- `pre_close_no_open`：距离 MT5 收盘小于配置分钟数，禁止新开仓但允许平仓。
- `post_open_cooldown`：刚开盘冷却，禁止新开仓，等待点差和流动性恢复。
- `reduce_only`：MT5 当前只允许减仓/平仓。
- `quote_only`：有报价但不在交易时段，不生成可执行机会。
- `closed`：不在报价时段。
- `unknown`：无法读取 MT5 会话状态，默认禁止新开仓。

扫描器会在生成机会前检查 MT5 动作权限：

```text
long_mt5_short_hyperliquid 开仓需要 MT5 can_open_long
long_hyperliquid_short_mt5 开仓需要 MT5 can_open_short
平仓时分别检查 can_close_long / can_close_short
```

当 MT5 不可报价时，系统只记录拒绝快照并跳过可交易机会扫描；当处于 `quote_only/pre_close_no_open/post_open_cooldown/reduce_only` 时，可以保留价差快照用于研究，但不会生成候选机会。

每个品种可配置：

- `mt5_pre_close_no_open_minutes`：盘尾禁止新开仓分钟数，默认 15。
- `mt5_post_open_cooldown_minutes`：开盘冷却分钟数，默认 10。
- `allow_hold_through_mt5_close`：是否允许跨 MT5 休市持仓，默认关闭；当前首版仅保存配置，自动持仓决策后续接入对冲组巡检。

当前本机 MetaTrader5 Python 包未提供 `symbol_info_session_trade()` / `symbol_info_session_quote()` 时，系统会退化为 `mt5_tick_trade_mode_fallback`：

- 使用 `symbol_info().trade_mode` 判断是否 full/long only/short only/close only/disabled。
- 使用 `symbol_info_tick()` 的 bid/ask 和 tick 更新时间判断是否仍有有效报价。
- tick 超过 `MT5_SESSION_TICK_STALE_SECONDS` 未更新时，按休市或不可交易处理。
- 该模式无法提供精确 `seconds_to_close` / `seconds_to_open`，盘尾和开盘冷却只能在后续接入券商 session API 或手动交易时段表后精确生效。

## 品种规格

前端设置页支持品种映射 CRUD，并可以从 MT5 经纪商配置同步规格。

系统内部的 `min_order_size` 表示最终有效基础币数量，不直接等于 MT5 手数，也不直接等于 Hyperliquid 的最小名义额。同步 MT5 时按以下方式换算：

```text
内部最小量 = MT5 volume_min * trade_contract_size
内部数量步进精度 = decimal_places(MT5 volume_step * trade_contract_size)
合约乘数 = MT5 trade_contract_size
价格精度 = MT5 digits
最小跳动 = MT5 trade_tick_size 或 point
```

最终最小量按以下方式计算：

```text
hyper_min_by_notional = hyperliquid_min_notional / hyperliquid_mid_price
min_order_size = max(mt5_min_base_size, hyperliquid_min_base_size, hyper_min_by_notional)
```

例如某券商 `ETHUSD volume_min=0.1` 且 `trade_contract_size=1`，则内部最小量会同步为 `0.1 ETH`。如果另一个平台同样显示 `0.01`，但合约大小不同，必须先换算成同一基础币数量再比较。

Hyperliquid 侧的 `10` 通常应理解为最小名义金额，例如 10 USD，而不是 10 BTC。系统会用当前 Hyperliquid mid price 折算成基础币数量后再和 MT5 约束取最大值。

## 扫描流程

1. 加载手动品种映射。
2. 从实时行情缓存读取同步后的 Hyperliquid 与 MT5 bid/ask。
3. 如果两边报价未对齐或已过期，记录拒绝原因，不产生可执行机会。
4. 计算两个方向的价差。
5. 估算手续费、资金费、点差、佣金、隔夜费、滑点和汇率损耗。
6. 更新 `spread_current` 当前价差状态，供价差扫描页实时读取。
7. 当扣除成本后仍有利润时，同步当前候选池；价差回落或方向切换时，对应未执行机会会移出当前候选池。
8. 按 `SPREAD_BUCKET_SECONDS` 聚合内存样本，并按 `SPREAD_HISTORY_INTERVAL_SECONDS` 低频写入 `spread_buckets` / `spread_snapshots`，供价差研究使用。

扫描频率和历史落库频率分离：

- `SCANNER_INTERVAL_MS`：大于 0 时启用毫秒级扫描，例如 100 表示 100ms 一次。
- `SCANNER_INTERVAL_SECONDS`：未设置毫秒扫描时使用的秒级扫描间隔。
- `SPREAD_HISTORY_INTERVAL_SECONDS`：历史快照和 market snapshot 落库间隔。
- `SPREAD_BUCKET_SECONDS`：价差研究聚合桶大小。
- `STREAM_INTERVAL_MS`：SSE 推送间隔，默认 1000ms。

高频扫描只更新当前状态和候选池，数据库历史按聚合桶低频写入，避免 100ms 扫描时产生千万级原始行。

前端价差扫描、候选机会和账户页通过 SSE 接收最新状态，不再依赖页面定时轮询；价差研究只接收 bucket 变更信号后重拉当前图表数据。

## 成本模型

当前成本模型优先读取真实/准实时数据，读取失败时使用 `.env` 默认值兜底：

- 名义价值口径：系统统一使用 USD 名义价值做风控和收益计算。MT5 品种会先按 `symbol_info()` 同步到的 `currency_profit/trade_contract_size/volume_min/volume_step` 计算 MT5 手数和本币名义价值，再用实时 FX 换算成 USD。
- 分平台数量：MT5 使用 `mt5_quantity`，Hyperliquid 使用 `hyperliquid_quantity`。对于 JP225 这类 JPY 计价指数，MT5 1 手约等于 `JP225价格 / USDJPY` 的 USD 名义价值；Hyperliquid 数量按 MT5 每点 USD 价值对齐，不再假设两边数量相同。
- 第一版主触发参数是 `default_notional` 和统计信号：前者表示单次目标 USD 名义价值，后者用历史价差自动计算 `reachable_entry` 可达入场线。固定净利润和年化只作为样本不足时的回退规则。
- 统计入场线：默认用最近 `1h` 样本，`reachable_entry = max(p75(spread), mean + 1σ)`。当前价差达到可达入场线、覆盖 `p90(cost)` 成本保护线，并满足最小总利润后，才进入 `executable`。
- 统计退出线：默认使用同一窗口的低分位回落目标，并用开仓价差反推利润保护上限：`exit_target = min(p25(spread), entry_spread - p90(unit_cost) - 每份利润缓冲)`。当利润保护上限小于等于 0 时退出线记为 0，表示当前参数下不能生成有效自动平仓线。开仓时会把 `entry_spread`、`reachable_entry`、`exit_target` 和 `overheat` 固定保存到对冲组，避免持仓过程中目标线随滚动窗口频繁漂移。
- Hyperliquid 深度：扫描时如果 quote 中有 `depth_notional`，且目标 USD 名义价值超过顶层可用深度，机会只保留为 candidate，不进入 executable。完整 order book 分层吃单模拟后续再接。
- Hyperliquid 手续费：优先用 `userFees` 的 `userCrossRate/userAddRate` 作为账户基础 taker/maker fee；未配置钱包地址时使用 `.env` 默认费率。
- HIP-3 手续费：如果交易对是 `dex:symbol` 格式，会读取 `metaAndAssetCtxs` 的资产元数据；`xyz` DEX 会根据 `growthMode` 自动套用 trade[XYZ] growth/standard 费率倍数，避免把普通 Hyperliquid 费率直接套到 JP225/SP500 这类品种。
- Hyperliquid 开平仓手续费：默认按 `HYPERLIQUID_FEE_ROUND_TRIPS=2` 计入一开一平两次 taker fee。
- 如果交易对配置为 Hyperliquid maker，开仓或平仓对应腿会使用 maker fee 估算。
- Hyperliquid 买卖价差：按 `(HL ask - HL bid) * quantity` 计入一买一卖价差损耗。
- Hyperliquid funding：优先用 `metaAndAssetCtxs` 中对应合约的 `funding`，按预计持仓小时数折算。
- MT5 点差成本：按实时 `bid/ask` 点差折算。
- MT5 点差返佣：按 `MT5_SPREAD_REBATE_RATE` 抵扣点差成本，当前默认为 20%。
- MT5 隔夜费：当前账户 `MT5_SWAP_FREE=true`，默认不计隔夜费；关闭后再按 `symbol_info()` 的 `swap_long/swap_short/swap_mode/point/trade_contract_size` 估算。
- MT5 佣金：当前账户默认 `MT5_DEFAULT_COMMISSION_RATE=0`。
- 滑点：当前默认 `DEFAULT_SLIPPAGE_BPS=0`，实盘前可用模拟账户成交回报校准。
- 汇率损耗：当前默认 `DEFAULT_FX_COST_RATE=0`。

MT5 当前券商的 BTCUSD/ETHUSD `swap_mode=1`，按点数模式估算：

```text
swap_cost = abs(selected_swap) * point * trade_contract_size * quantity * holding_days
```

其中 `selected_swap` 根据 MT5 持仓方向选择 `swap_long` 或 `swap_short`。

Funding 和 swap 按持仓方向计入：

```text
Hyperliquid funding cost = notional * funding_rate * holding_hours * side_sign
side_sign = +1 多头支付/收取方向
side_sign = -1 空头反向

MT5 swap cost = -swap_pnl
```

因此正 funding 时，做空 Hyperliquid 会降低成本；MT5 swap 为正时会降低成本，为负时会增加成本。

## 信号状态

- `rejected`：扣除成本后无利润。
- `candidate`：有利润，但未达到执行阈值。
- `executable`：净利润和年化收益均达到阈值。
- `executed`：机会已经创建过对冲组。

价差扫描、候选机会和价差研究页面的毛价差、成本、净利润按每 1 份基础资产展示。数据库同时保留两套口径：

- `unit_cost` / `unit_net_profit`：每 1 份基础资产的成本和净利润，供页面展示和价差研究使用。
- `total_cost` / `net_profit`：按本次扫描实际 `quantity` 估算出的总成本和总净利润，供执行、风控和对冲组记录使用。

## 价差研究与回归评估

系统新增价差研究层，暂时只用于观察和人工判断，不直接触发自动下单。

价差研究优先使用扫描器写入的 `spread_buckets` 聚合历史；没有聚合数据时才回退 `spread_snapshots`。它和“价差扫描”页面的实时最新视图不是同一个展示口径。每个品种、每个方向会基于历史聚合数据计算：

- 当前价差。
- 历史均值和标准差。
- Z-Score。
- 当前价差历史分位数。
- 估算半衰期。
- 5/15/30/60 分钟历史回归概率。
- 最大不利扩张。
- 平均成本线。
- 机会评分。

前端展示价差、均值、标准差、成本线和图表 tooltip 时使用自适应小数位；EUR 等小价差品种会保留更多小数，避免固定两位小数把有效波动显示成 `0.00`。

状态含义：

- `no_data`：没有历史快照。
- `watch_only`：样本不足，只观察。
- `normal_range`：当前价差未显著偏离历史分布。
- `slow_reversion`：估算回归较慢。
- `too_risky`：历史同类偏离回归概率偏低。
- `mean_reversion`：价差偏离具备均值回归观察价值。

曲线接口会在后端按时间范围降采样，避免长周期数据压垮前端：

```text
15m  -> 约 900 点
1h   -> 约 720 点
4h   -> 约 960 点
24h  -> 约 1440 点
7d   -> 约 2016 点
```

当前回归概率是基于历史快照的经验统计，不代表确定性收益。品种样本不足、流动性突变、平台报价异常、资金费突变或 MT5 经纪商点差扩大时，统计结果需要降权处理。

## 报价领先滞后监控

系统新增“报价领先滞后”页面，用于观察同一个内部品种中 Hyperliquid 与 MT5 谁先发生明显跳动，另一边是否在指定时间窗口内跟随。

第一版只使用内存 `QuoteCache` 中最近报价历史，不把每个 tick 写入数据库。默认缓存每个平台每个品种最近 5000 条报价。

核心判断：

```text
leader 在相邻两条报价之间跳动超过 threshold_bps 或 min_move
然后观察 follower 是否在 max_lag_ms 内同方向跟随
如果跟随幅度达到 leader_move_bps * follow_ratio，则记为一次跟随事件
```

页面展示：

- HL 最新 mid / MT5 最新 mid。
- HL -> MT5 平均滞后、P90 滞后、跟随率。
- MT5 -> HL 平均滞后、P90 滞后、跟随率。
- 标准化 mid 曲线和两边 mid 差。
- 跳动事件列表：领先方、跟随方、方向、滞后毫秒、跳动幅度、期间最大 mid 差。

该页面不能直接识别 MT5 经纪商的上游 LP 或 Hyperliquid HIP-3 的完整 oracle 源头，只用于判断报价行为：

```text
谁更常先动
另一边平均慢多久
这种慢是否稳定
滞后期间是否形成可交易价差
```

## 执行模式

- `dry_run`：只记录计划，不用于真实下单。
- `paper`：使用 PaperAdapter 模拟成交。
- `live`：进入实盘路径，但必须先开启实盘开关。

真实下单路径仍需要显式开关保护。MT5 实盘下单需要 `MT5_LIVE_ORDER_ENABLED=true`；NautilusTrader Hyperliquid 实盘提交需要同时开启 `NAUTILUS_HYPERLIQUID_ENABLED=true` 和 `NAUTILUS_HYPERLIQUID_SUBMIT_ENABLED=true`。

执行层已经接入 NautilusTrader Hyperliquid 边界：上层对冲组管理只创建双腿意图并维护 hedge lifecycle；单腿订单提交先经过 `ExecutionGateway`，由 gateway 输出统一的 `OrderEvent` 和 `FillEvent`，再写入 `orders` / `fills`。默认实现 `AdapterExecutionGateway` 仍桥接现有 Hyperliquid/MT5 adapter；当 `NAUTILUS_HYPERLIQUID_ENABLED=true` 且平台为 Hyperliquid 时，`build_execution_gateway()` 会切换到 `NautilusHyperliquidGateway`。MT5 腿仍由现有 adapter 执行，不由 NautilusTrader 接管。

NautilusTrader Hyperliquid gateway 不改变对冲组语义。它会把内部 `BTC`、`xyz:JP225` 等符号转换成 NautilusTrader instrument id，例如 `BTC-USD-PERP.HYPERLIQUID`、`xyz:JP225-USD-PERP.HYPERLIQUID`。打开真实提交开关后，gateway 会启动 NautilusTrader TradingNode，注册 bridge Strategy，并把 market/limit 单提交给 NautilusTrader；accepted/filled/rejected 事件会映射回系统订单结果。真实运行前必须先安装 `nautilus_trader`，并配置 testnet 或小额主网凭证。

实盘订单只有出现 `filled` 或 `partially_filled` 且成交数量大于 0 时，才会被视为已经产生持仓效果。`accepted` / `submitted` 只表示订单已进入外部系统，开仓流程会保持 `opening` 并写入待成交事件，不会把 0 成交对冲组标记为 `open`。

## Maker 执行策略

品种映射支持配置执行方式：

- `taker_taker`：Hyperliquid 和 MT5 都按市价执行。
- `hyper_maker_mt5_taker`：先在 Hyperliquid 下 post-only 限价单，成交后再用 MT5 市价对冲。

Hyperliquid maker 参数：

- `hl_maker_offset_bps`：挂单相对 bid/ask 的偏移。
- `hl_order_ttl_seconds`：挂单等待时间。
- `hl_unfilled_action`：未成交时撤单放弃或转市价兜底。
- `single_leg_action`：单腿异常动作。默认 `manual_intervention`；设置为 `auto_close` 或 `reverse_filled_leg` 时，reconciler 会在单腿成交后反向冲销已成交腿。

当前 Paper 模式会模拟 post-only 成交和未成交；live 模式仍需要在 Hyperliquid 适配器中接入真实 post-only 下单、撤单和成交查询。

## 自动纸面执行

自动执行第一版只建议用于 `paper` 模式，默认关闭。调度器每次价差扫描后会尝试执行自动执行器，但只有当前机会满足 `executable` 状态且通过额外保护时才会触发：

- `auto_execute_enabled`：开启自动执行。
- `auto_execute_paper_only`：开启后只允许 `execution_mode=paper` 时自动执行。
- `auto_execute_confirm_ticks`：机会连续确认次数。
- `auto_execute_min_hold_ms`：机会至少持续多少毫秒。
- `auto_execute_cooldown_seconds`：同品种同方向执行失败或成功后的冷却时间。
- `auto_execute_max_per_symbol_open_groups`：单品种未平对冲组上限。
- `auto_execute_max_global_open_groups`：全局未平对冲组上限。
- `auto_execute_min_net_profit`：自动执行额外净利润门槛；为 0 时沿用策略最小净利润。

自动执行不会绕过执行引擎。触发后仍会调用 `open_hedge_group()`，继续执行严格行情同步、风控、资金和 MT5 时段检查。抢占中的机会会标记为 `executing`，成功后标记为 `executed`。

Paper 延迟模拟：

- `paper_decision_delay_ms_min/max`：后端发现机会到发单前的随机决策延迟。
- `paper_hyperliquid_latency_ms_min/max`：Hyperliquid paper 下单随机延迟。
- `paper_mt5_latency_ms_min/max`：MT5 paper 下单随机延迟。

Paper 成交会在延迟结束后重新读取 `quote_cache` 的最新 bid/ask 成交，而不是使用机会产生时的旧价格，用于暴露短窗口机会在真实执行延迟下消失的情况。

## 自动纸面平仓

自动平仓首版只作用于 `paper` 对冲组，默认使用开仓时保存的统计退出线，不直接对 live 账户发反向单。

平仓判断流程：

1. 调度器每次扫描后读取未平 `paper` 对冲组。
2. 使用严格同步报价计算当前平仓价差：
   - `long_hyperliquid_short_mt5`：`MT5 ask - HL bid`。
   - `long_mt5_short_hyperliquid`：`HL ask - MT5 bid`。
3. 用开仓价差估算当前利润：

```text
estimated_profit = (entry_spread - close_spread) * hyperliquid_quantity - open_cost
```

4. 当 `close_spread <= exit_target` 且 `estimated_profit >= auto_close_min_profit` 时，系统用反向订单同时平掉两边；paper 组直接模拟成交，live 组只有在 `auto_close_live_enabled=true` 且 `live_trading_enabled=true` 时才提交实盘平仓腿。
5. 如果超过 `max_holding_minutes` 且利润达标，也允许按时间退出。

相关参数：

- `auto_close_enabled`：是否启用自动平仓调度。
- `auto_close_live_enabled`：是否允许自动平仓处理 live 对冲组，默认关闭；开启后仍需系统实盘总开关和平台发单开关。
- `exit_target_percentile`：退出线低分位数，默认 `0.25`，用于寻找更低的回落目标。
- `auto_close_unit_profit_buffer`：每份平仓利润缓冲，默认 `20`；该值与 `entry_spread`、`unit_cost` 同量纲，按每 1 份 Hyperliquid 数量的价差利润计算。退出线会被限制在 `entry_spread - 单位成本 - 缓冲` 以下，避免小价差品种被 `20` 这类大数值抬出 `20.xx` 的无效退出线。
- `auto_close_min_profit`：自动平仓最低估算利润，默认 `0`。
- `max_holding_minutes`：最大持仓时间，超时且利润达标时允许退出。

自动平仓成功后，对冲组会写入 `close_reason`、`realized_pnl`，并记录两条反向订单和成交。live 自动平仓提交后可能先进入 `closing`，由 execution reconciler 回查确认最终成交。单腿平仓异常会进入 `manual_intervention`。

手工关闭对冲组会按 `execution_mode` 执行反向订单：`paper` 走模拟成交，`live` 要求 `live_trading_enabled=true`，并分别通过 NautilusTrader Hyperliquid gateway 和 MT5Adapter 提交平仓腿。live 平仓腿会带 `reduce_only` 语义；MT5Adapter 在 hedging 账户下会先用 `positions_get(symbol=...)` 找到被减仓方向的持仓 ticket，并在 `order_send` 请求里写入 `position`，找不到可平仓持仓或请求数量超过当前持仓数量时拒绝发单，避免反向单变成新增仓位。双边成交后状态变为 `closed`；任一边只返回 `accepted/submitted` 且未成交时，状态保持 `closing`，不会把待成交订单当成已平仓；单边成交会进入 `manual_intervention`。

后台调度会运行 execution reconciler，周期性回查 `opening` / `closing` 对冲组里的 pending 订单。回查确认双边成交后，开仓会推进到 `open`，平仓会推进到 `closed` 并补写成交记录；回查发现单边成交和另一边失败时会进入 `manual_intervention`。reconciler 还会刷新 live positions：Hyperliquid 从 `clearinghouseState` 读取 perp 仓位，MT5 从终端 `positions_get()` 读取仓位，并校验已 `closed` 的 live 对冲组是否仍有对应符号残仓；发现残仓时会发告警并把对冲组拉回 `manual_intervention`。如果账户里存在无法匹配任何 live 对冲组的 Hyperliquid/MT5 仓位，系统会发出“外部孤儿仓位”告警，避免裸仓脱离对冲组管理。该回查只消费交易所/adapter 明确返回的订单和仓位状态，不用本地猜测成交。

管理员可手工触发 execution reconciler：对冲组页面的“同步执行状态”按钮会调用 `POST /api/execution/reconcile`，用于外部人工处理订单或仓位后立即刷新系统状态。

如果 readiness 或 reconciler 发现外部孤儿仓位，管理员可在仓位页点击“接管”，或调用 `POST /api/positions/{id}/adopt`。接管会按品种映射把单腿外部仓位创建为 `execution_mode=live`、`status=manual_intervention` 的对冲组，并记录 `adopted_external_position` 事件；该组不是完整双腿套利，只表示系统已经把外部仓位纳入生命周期。后续手工平仓只会对非零数量的平台腿提交 reduce-only 反向订单，不会为不存在的另一腿发单。

如果回查发现一条腿已经成交、另一条腿仍处于 `accepted/submitted/pending`，系统会先尝试撤销未成交腿。默认 `single_leg_action=manual_intervention` 时，对冲组进入 `manual_intervention` 等人工确认；如果品种映射配置为 `auto_close` 或 `reverse_filled_leg`，系统会对已成交腿提交一条反向市价冲销单。开仓阶段冲销成交后，对冲组标记为 `failed`，表示没有留下有效对冲组；平仓阶段冲销成交后，对冲组回到 `open`，表示原对冲关系已恢复。补偿单未成交或被拒绝时仍进入 `manual_intervention`。

Nautilus Hyperliquid gateway 会先查同进程 bridge Strategy 的本地订单事件；如果本地 cache 查不到，会用 Hyperliquid `orderStatus` 主动查询外部订单，并在成交时用 `userFills` 回填成交量、均价和手续费。reconciler 还会读取账户级 `openOrders` / `userFills` 快照，用于恢复已有外部订单号的 pending 单；如果本地 Hyperliquid pending 单缺少 `external_order_id`，且账户快照中只有一条 symbol、side、quantity、时间都匹配的记录，也会补回外部订单号和成交明细。主动回查需要配置实际交易账户地址，API wallet/agent private key 场景应填写 `HYPERLIQUID_ACCOUNT_ADDRESS`。如果外部订单状态仍无法重建，reconciler 会先保留 pending 状态继续等待；超过 `execution_reconcile_pending_stale_seconds` 后仍无法确认时，会尝试撤销该订单并把对冲组升级为 `manual_intervention`。

实盘执行前可通过设置页或 `GET /api/settings/live-readiness` 做就绪检查。该检查不会发单，只验证全局 live 开关、NautilusTrader Hyperliquid 运行依赖与提交开关、Hyperliquid 账户回查地址和 `clearinghouseState` 只读连通性、MT5 Python 包和 `account_info()` 只读连通性、MT5 实盘开关、启用品种映射、MT5 合约规格、已同步 live 仓位归属以及单腿自动补偿配置。如果 `positions` 表中存在未归属任何 live 对冲组的 Hyperliquid/MT5 仓位，或已关闭 live 对冲组仍有残余仓位，就绪检查会返回 `block`。`open_hedge_group()` 和 live `close_hedge_group()` 会在真实提交前执行同一套检查；存在 `block` 项时会直接拒绝 live 开仓或平仓。

## 下单前复核

候选机会不能直接下单，只有 `executable` 状态允许执行。执行引擎会再次读取严格同步报价：

- 两边报价时间差必须小于 `strict_quote_sync_ms`。
- 两边报价最大年龄必须小于 `quote_stale_ms`。
- 如果同步失败，写入风控事件和站内告警。
- 同一个机会执行后会标记为 `executed`。
