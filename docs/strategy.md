# 策略文档

首版策略以手动品种映射为核心，不做自动全市场匹配。系统运行时只扫描数据库 `symbol_mappings` 表中 `enabled: true` 的交易对；`config/symbol_mappings.yaml` 只作为首次初始化数据库的种子文件。

## 实时行情与时间对齐

行情获取和价差计算已经拆开：

- 行情 worker 持续更新内存 `QuoteCache`。
- 扫描器不直接请求平台报价，只读取 `QuoteSynchronizer` 输出的同步报价对。
- 宽松扫描使用 `loose_quote_sync_ms`，默认允许更大的时间窗口，只用于发现候选。
- 执行前复核使用 `strict_quote_sync_ms` 和 `quote_stale_ms`。如果缓存报价时间差或新鲜度不达标，执行引擎会主动刷新一次 Hyperliquid HTTP L2 和 MT5 tick；Hyperliquid 运行期不做 HTTP 轮询，保留请求额度给下单前复核。
- 每条报价保存 `local_recv_ts`、可用时保存 `exchange_ts`，并记录 `source` 和 `sequence` 便于排查。

默认 `quote_source_mode=paper` 时，系统用 Paper 行情 worker 模拟两个平台的实时报价。切换 `quote_source_mode=live` 后：

- Hyperliquid 默认只使用原生 WebSocket 订阅 `l2Book`；`HYPERLIQUID_L2BOOK_FAST_ENABLED=true` 时订阅携带 `fast: true`，用于扫描浅层高频盘口。
- `xyz:*` 这类 HIP-3 DEX 品种同样通过原生 Hyperliquid `l2Book` WS 订阅维护报价。
- HTTP `l2Book` 不再作为后台行情兜底，只在执行前主动刷新时调用一次，避免平时请求耗尽额度导致下单前复核遇到 429。
- MT5 使用 Python API 高频轮询 `symbol_info_tick()`。

MT5 的 Python API 还提供 Depth of Market 相关函数：`market_book_add()`、`market_book_get()`、`market_book_release()`。这类盘口数据依赖券商是否提供对应品种的 Market Depth；对很多外汇或 CFD 品种，可能只有 tick bid/ask，没有可用深度。

高频扫描会实时读取 `QuoteCache` 计算当前价差，但配置类数据和统计线会尽量走内存快照，避免 PostgreSQL 网络往返进入扫描热路径。启用品种映射和策略设置会缓存 2 秒；设置页修改策略、品种映射、同步 MT5 规格或同步交易时段后会立即清理对应缓存。统计入场线、成本保护线、退出线和过热线不再在扫描热路径里查库重算，后台会按 `SIGNAL_STATS_CACHE_TTL_MS` 独立刷新统计线到内存，默认 10000ms；扫描线程只读取内存快照。缓存只保存历史样本统计线，不缓存最终信号结果；每轮扫描仍会用当前价差、当前净利润和当前报价状态重新判断是否 candidate/executable。冷启动或某个品种/方向还没有统计快照时，扫描会同步兜底计算一次，之后由后台刷新接管。

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

扫描器会在生成机会前检查 MT5 动作权限，执行入口 `open_hedge_group()` 也会在创建对冲组和提交 Hyperliquid 腿之前再次检查同一权限：

```text
long_mt5_short_hyperliquid 开仓需要 MT5 can_open_long
long_hyperliquid_short_mt5 开仓需要 MT5 can_open_short
平仓时分别检查 can_close_long / can_close_short
```

当 MT5 不可报价时，系统只记录拒绝快照并跳过可交易机会扫描；当处于 `quote_only/pre_close_no_open/post_open_cooldown/reduce_only` 时，可以保留价差快照用于研究，但不会生成候选机会。若已有旧机会进入执行，或 MT5 会话状态在扫描后到下单前发生变化，执行入口会拒绝开仓、写回机会拒绝原因和风控事件，并且不会创建对冲组或先提交 Hyperliquid 腿。部分 MT5 Python 包不暴露 `symbol_info_session_trade()` 时，执行入口还会在 Hyperliquid 腿之前对即将提交的 MT5 市价单执行 `order_check`，用服务器返回的 retcode 兜底识别盘前只平仓、只允许卖出或 session closed 等状态。

系统会在内存中维护 `mt5_tradability_cache`，后台按 `MT5_TRADABILITY_REFRESH_SECONDS` 周期对每个启用品种的 MT5 `buy/sell` 市价单做 `order_check` 探测，缓存字段包括方向、是否允许、retcode、原因、探测手数和缓存年龄。扫描器只读取该缓存，不在每轮扫描里同步打 MT5 服务器；缓存未初始化、过期或方向不允许时，机会会被标记为 rejected。服务启动时会先等待行情种子和 MT5 交易能力首轮刷新，再运行首轮扫描，自动开仓等调度器下一轮。执行入口不会用后台探测手数缓存替代最终检查，真正提交 Hyperliquid 腿之前始终会对本次实际 MT5 symbol、side 和 quantity 执行一次 `order_check`，避免最小手数可开但实际手数被服务器拒绝。若实际 `order_send` 仍返回 `retcode=10044`，系统会把该 MT5 symbol/side 写入持久隔离，默认 6 小时内扫描和执行都不再放行该方向，防止同一盘前/只平仓窗口反复打出单边异常。

每个品种可配置：

- `mt5_pre_close_no_open_minutes`：盘尾禁止新开仓分钟数，默认 15。
- `mt5_post_open_cooldown_minutes`：开盘冷却分钟数，默认 10。
- `allow_hold_through_mt5_close`：是否允许跨 MT5 休市持仓，默认关闭；当前首版仅保存配置，自动持仓决策后续接入对冲组巡检。
- `MT5_TRADABILITY_CACHE_TTL_MS`：MT5 交易能力缓存有效期，默认 15000ms；刷新周期默认 5 秒，TTL 留出调度抖动空间，避免刚超过 5 秒就把机会判为缓存过期。
- `MT5_TRADABILITY_REFRESH_SECONDS`：后台刷新 MT5 交易能力缓存周期，默认 5 秒。
- `MT5_TRADE_REJECT_QUARANTINE_SECONDS`：MT5 实际下单返回只允许平仓等 10044 拒单后的方向隔离时长，默认 21600 秒。

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
6. 更新 `spread_current` 当前价差状态，供链路监控和重启兜底读取。
7. 当扣除成本后仍有利润时，同步当前候选池；价差回落或方向切换时，对应未执行机会会移出当前候选池。
8. 更新内存扫描状态，供链路监控和 SSE 实时推送直接读取；同时继续写入 `spread_current` / `arbitrage_opportunities` 作为执行、审计、自动执行和重启兜底。
9. 按 `SPREAD_BUCKET_SECONDS` 聚合内存样本，并按 `SPREAD_HISTORY_INTERVAL_SECONDS` 低频写入 `spread_buckets` / `spread_snapshots`，供价差研究使用。

扫描频率和历史落库频率分离：

- `SCANNER_INTERVAL_MS`：大于 0 时启用毫秒级扫描，例如 100 表示 100ms 一次。
- `SCANNER_INTERVAL_SECONDS`：未设置毫秒扫描时使用的秒级扫描间隔。
- `SPREAD_HISTORY_INTERVAL_SECONDS`：历史快照和 market snapshot 落库间隔。
- `SPREAD_BUCKET_SECONDS`：价差研究聚合桶大小。
- `STREAM_INTERVAL_MS`：SSE 推送间隔，默认 1000ms。

高频扫描只更新当前状态和候选池，数据库历史按聚合桶低频写入，避免 100ms 扫描时产生千万级原始行。

前端链路监控和账户页通过 SSE 接收最新状态，不再依赖页面定时轮询；扫描和候选机会实时视图已合并到链路监控，优先读取内存扫描状态，服务刚启动或扫描尚未完成时才回退数据库当前表。价差研究只接收 bucket 变更信号后重拉当前图表数据。

“链路监控”页面把实时行情、扫描结果、候选机会和对冲组生命周期合并成诊断视图。左侧行情管道按品种展示：

- Hyperliquid 报价年龄、来源、bid/ask。
- MT5 报价年龄、来源、bid/ask 和交易时段状态。
- HL 与 MT5 两边本地接收时间差。
- 最近扫描状态、扫描结果年龄和阻塞原因。
- 信号/候选机会状态，以及是否已经可通过 SSE/接口推送到前端。

诊断结果会返回 `blocked_stage` 作为主阻塞环节，同时返回 `blockers` 列表记录同一品种上的多个阻塞原因。例如 Hyperliquid 行情过期和 MT5 交易时段关闭可以同时出现；前端以主阻塞点为红色阀门，后续流程灰化，避免误以为后续扫描/信号仍在继续。

页面中的行情延迟和计算耗时分开展示：`HL age` / `MT5 age` 表示报价距当前诊断生成时间的年龄，`同步差` 表示两边报价进入本地缓存的时间差；`扫描耗时`、`成本`、`信号`、`入池同步` 来自扫描器每个品种本轮 `perf_counter()` 埋点，表示真实计算耗时。`结果 age` 单独表示当前扫描结果距诊断生成时间的年龄，不代表计算耗时或 SSE 推送耗时。

右侧“候选闸门与对冲池”只展示已经进入交易生命周期的活跃对冲组，不作为行情中枢。候选机会满足执行条件后进入执行闸门，成功开仓后进入对冲池；平仓完成后释放/归档。该视图用于区分：

- 行情管道为什么没有机会。
- 候选机会为什么还没执行。
- 已执行对冲组当前处于待执行、建仓中、持仓中、可平仓、平仓中还是异常/人工接管。

## 成本模型

当前成本模型优先读取真实/准实时数据，读取失败时使用 `.env` 默认值兜底：

- 名义价值口径：系统统一使用 USD 名义价值做风控和收益计算。MT5 品种会先按 `symbol_info()` 同步到的 `currency_profit/trade_contract_size/volume_min/volume_step` 计算 MT5 手数和本币名义价值，再用实时 FX 换算成 USD。
- 分平台数量：MT5 使用 `mt5_quantity`，Hyperliquid 使用 `hyperliquid_quantity`。对于 JP225 这类 JPY 计价指数，MT5 1 手约等于 `JP225价格 / USDJPY` 的 USD 名义价值；Hyperliquid 数量按 MT5 每点 USD 价值对齐，不再假设两边数量相同。
- 第一版主触发参数是 `default_notional` 和统计信号：前者表示单次目标 USD 名义价值，后者用历史价差自动计算 `reachable_entry` 可达入场线。统计模式下，某个品种/方向样本不足时只保留为 `candidate`，不会进入 `executable`；固定净利润和年化只在非统计信号模式下使用。
- 统计入场线：默认用最近 `1h` 样本，`reachable_entry = max(p75(spread), mean + 1σ)`。每个品种还可以配置 `min_entry_spread`（前端显示为“最小买入价差”），最终入场线取 `max(reachable_entry, min_entry_spread)`；当前价差达到最终入场线、覆盖 `p90(cost)` 成本保护线，并满足最小总利润后，才进入 `executable`。
- 统计退出线：默认使用同方向 `close_spread` 的低分位回落目标，并用开仓价差反推利润保护上限：`exit_target = min(p25(close_spread), entry_spread - p90(unit_cost) - 每份利润缓冲)`。每个品种还可以配置 `max_close_spread`（前端显示为“最大卖出价差”），最终退出线在有统计线时取 `min(exit_target, max_close_spread)`，缺少统计线时可直接使用 `max_close_spread` 兜底。每份利润缓冲默认 `0`，跨品种利润保护优先使用 USD 口径的 `auto_close_min_profit`。当利润保护上限小于等于 0 且没有品种退出线上限时，退出线记为 0，表示当前参数下不能生成有效自动平仓线。开仓时会把 `trigger_spread`、触发时 HL/MT5 bid/ask、`entry_spread`、最终 `entry_threshold`、最终 `exit_target` 和 `overheat` 固定保存到对冲组；双边成交后 `entry_spread` 会被真实成交价差覆盖，`trigger_spread` 和触发盘口保留机会触发时的原始状态，方便对比执行质量。

价差口径：

```text
long_hyperliquid_short_mt5:
entry_spread = MT5 bid - HL ask
close_spread = MT5 ask - HL bid
mid_spread = MT5 mid - HL mid

long_mt5_short_hyperliquid:
entry_spread = HL bid - MT5 ask
close_spread = HL ask - MT5 bid
mid_spread = HL mid - MT5 mid

spread_cost = close_spread - entry_spread
gross_spread = entry_spread（旧 API 兼容别名）
```

入场线 `reachable_entry / strong_entry / overheat` 只使用 `entry_spread` 历史分布；退出线只使用 `close_spread` 历史分布。价差研究页面可以切换 `入场价差 / 平仓价差 / Mid价差`，所有均值、分位、Z-Score 和回归概率都基于当前选择的口径。
- Hyperliquid 深度：扫描优先使用内存中的 L2 order book 多档盘口按目标 HL 数量模拟吃单；多档仍不够时机会只保留为 candidate，不进入 executable。仅在没有 L2 缓存时才回退到旧的顶层 `depth_notional` 判断。L2 模拟只遍历缓存的固定档位，计算量是 `品种数 * 档位数`，不在扫描热路径发起网络请求。
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

链路监控和价差研究页面的毛价差、成本、净利润按每 1 份基础资产展示。数据库同时保留两套口径：

- `unit_cost` / `unit_net_profit`：每 1 份基础资产的成本和净利润，供页面展示和价差研究使用。
- `total_cost` / `net_profit`：按本次扫描实际 `quantity` 估算出的总成本和总净利润，供执行、风控和对冲组记录使用。

## 价差研究与回归评估

系统新增价差研究层，暂时只用于观察和人工判断，不直接触发自动下单。

价差研究按时间范围选择数据源：`15m`、`1h`、`4h` 优先使用 `spread_snapshots` 原始点，保留短周期尖峰和回落形状；`24h`、`7d` 优先使用 `spread_buckets` 聚合历史，缺少对应数据时再回退另一种来源。它和“链路监控”的实时最新视图不是同一个展示口径。每个品种、每个方向会基于历史数据计算：

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
15m  -> 原始点统计，图表约 900 点
1h   -> 原始点统计，图表约 720 点
4h   -> 原始点统计，图表约 960 点
24h  -> 聚合桶统计，图表约 1440 点
7d   -> 聚合桶统计，图表约 2016 点
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
- `paper`：纸面账本执行。默认 Hyperliquid 腿使用本地 `QuoteCache` 的最新 bid/ask 撮合；MT5 腿向当前 MT5 demo 账户提交 `order_send`，因此会在 demo 终端产生真实模拟订单。若 `HYPERLIQUID_PAPER_LIVE_ORDER_ENABLED=true`，Hyperliquid 腿会向真实账户提交最小可成交量探针单，并用真实成交均价写入 paper 账本。
- `live`：进入实盘路径，但必须先开启实盘开关。

真实下单路径仍需要显式开关保护。MT5 实盘下单需要 `MT5_LIVE_ORDER_ENABLED=true`；Hyperliquid `execution_mode=live` 下单当前仍固定阻止。Hyperliquid paper-live 探针单只在 `execution_mode=paper` 且 `HYPERLIQUID_PAPER_LIVE_ORDER_ENABLED=true` 时启用，仍会保留 paper 对冲组账本语义。

启用完整 paper 模拟前必须满足 `GET /api/settings/paper-readiness`：存在启用的品种映射，`MT5_DEMO_ORDER_ENABLED=true`，并且 MT5 `account_info().trade_mode` 是 demo。`MT5_LOGIN`、`MT5_PASSWORD`、`MT5_SERVER` 是唯一的 MT5 登录配置；如果配置了 `MT5_LOGIN` 或 `MT5_SERVER`，paper demo 下单前会要求当前账户 login/server 与它们一致。任一检查失败时，paper 开仓和平仓会直接拒绝执行。

执行层通过 `ExecutionGateway` 输出统一的 `OrderEvent` 和 `FillEvent`，再写入 `orders` / `fills`。`build_execution_gateway()` 当前统一返回 `AdapterExecutionGateway`；Hyperliquid paper 默认由本地 adapter 用最新报价撮合，开启 paper-live 探针后由真实最小量订单提供成交均价，MT5 腿仍由现有 adapter 执行。

默认开仓和平仓主路径采用顺序编排：先提交 Hyperliquid 腿；只有该腿返回 `filled` 或 `partially_filled` 且成交数量大于 0 时，才按成交比例提交 MT5 腿。`accepted` / `submitted` 只表示订单已进入外部系统，开仓流程会保持 `opening`、平仓流程会保持 `closing`，不会把 0 成交订单当作已开仓或已平仓。

当 `execution_mode=paper`、`HYPERLIQUID_PAPER_LIVE_ORDER_ENABLED=true` 且 `PAPER_LIVE_PARALLEL_EXECUTION=true` 时，系统在严格行情复核通过后会并发提交 Hyperliquid 最小真实探针单和 MT5 demo 单，不再等待 HL 回执后才提交 MT5。两边回执返回后再写入 `orders` / `fills`。如果只有一边成交，会立即对已成交腿提交反向冲销：开仓阶段使用 reduce-only 平掉新增腿；平仓阶段反向恢复已平掉的腿，并记录 `parallel_single_leg_compensation` 事件。

## Maker 执行策略

品种映射支持配置执行方式：

- `taker_taker`：Hyperliquid 和 MT5 都按市价执行，但仍按“Hyperliquid 先成交、MT5 后补腿”的顺序编排。
- `hyper_maker_mt5_taker`：先在 Hyperliquid 下 post-only 限价单，成交后再用 MT5 市价对冲。

Hyperliquid maker 参数：

- `hl_maker_offset_bps`：挂单相对 bid/ask 的偏移。
- `hl_order_ttl_seconds`：挂单等待时间。
- `hl_unfilled_action`：未成交时撤单放弃或转市价兜底。
- `single_leg_action`：单腿异常动作。默认 `manual_intervention`；设置为 `auto_close` 或 `reverse_filled_leg` 时，reconciler 会在单腿成交后反向冲销已成交腿。

当前 Paper 模式的 Hyperliquid post-only、market/limit 默认由本地 adapter 处理；MT5 侧由 broker demo 账户返回成交结果。Hyperliquid 市价单优先使用最新 L2 order book 多档模拟吃单均价，L2 不足时返回未成交；没有 L2 缓存时才回退到 `QuoteCache` 的 best bid/ask。post-only 限价单如果会立即吃单则拒绝，否则保持未成交或按后续逻辑处理。Hyperliquid paper 成交手续费复用策略成本侧的账户有效 taker/maker fee，优先按 `venue_symbol` 识别 `xyz:*` HIP-3 低费率，不再使用固定 `0.00035`。

开启 `HYPERLIQUID_PAPER_LIVE_ORDER_ENABLED=true` 后，Hyperliquid paper 腿不再使用本地 L2 模拟价格，而是读取交易所 `meta` 中该资产 `szDecimals` 和 `allMids`，按 `max(10^-szDecimals, HYPERLIQUID_DEFAULT_MIN_NOTIONAL / mid)` 向上取整到数量步进，提交最小真实探针单。开仓探针按 paper 腿方向提交；平仓探针走 reduce-only market close。探针真实成交量只用于产生价格和外部订单号，写入 `fills` 的数量仍是策略目标数量，避免 MT5 补腿比例、价差和 PnL 被探针数量缩小。

`PAPER_LIVE_PARALLEL_EXECUTION` 默认开启，用于降低“HL 先成交、MT5 后补腿”的串行延迟。若需要回到保守验证模式，可设置为 `false`，系统会恢复 HL 先确认成交再提交 MT5 的顺序编排。

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

Paper 成交会在延迟结束后重新读取 `quote_cache` 的最新 bid/ask 成交，而不是使用机会产生时的旧价格；默认 Hyperliquid paper 腿在发单前还会主动刷新一次 HTTP `l2Book`，并用刷新后的 bid/ask 撮合，用于暴露短窗口机会在真实执行延迟下消失的情况。执行前严格同步如果已经通过，通常只有这一次 HL `l2Book`；如果严格同步失败，则会先刷新一次 HL `l2Book` + MT5 tick 复核，真正提交 HL paper 腿前再刷新一次 HL `l2Book`。开启 paper-live 探针后，HL 腿价格来自真实探针成交回报，不再依赖本地 L2 模拟成交均价。

对冲组会保存两个入场价差口径：

- `trigger_spread`：机会触发/入池时的价差，不再被覆盖。
- `trigger_hyperliquid_bid` / `trigger_hyperliquid_ask` / `trigger_mt5_bid` / `trigger_mt5_ask`：机会触发/入池时用于计算 `trigger_spread` 的两边盘口价格。
- `entry_spread`：初始等于触发入场价差；双边开仓都确认成交后，系统会用 `fills` 中两腿成交均价回写，使“开仓价差”变成真实成交价差。

```text
long_hyperliquid_short_mt5:
真实开仓价差 = MT5 开仓卖出成交均价 - HL 开仓买入成交均价

long_mt5_short_hyperliquid:
真实开仓价差 = HL 开仓卖出成交均价 - MT5 开仓买入成交均价
```

## 自动纸面平仓

自动平仓首版只作用于 `paper` 对冲组，默认使用开仓时保存的统计退出线，不直接对 live 账户发反向单。

平仓判断流程：

1. 调度器每次扫描后读取未平 `paper` 对冲组。
2. 使用严格同步报价计算当前平仓价差：
   - `long_hyperliquid_short_mt5`：`MT5 ask - HL bid`。
   - `long_mt5_short_hyperliquid`：`HL ask - MT5 bid`。
3. 用真实开仓价差和当前平仓价差估算当前利润：

```text
estimated_profit = (entry_spread - close_spread) * hyperliquid_quantity - fees - funding - swap
```

4. 当 `close_spread <= exit_target` 且 `estimated_profit >= auto_close_min_profit` 时，系统先提交 Hyperliquid 反向平仓腿，确认成交后再按比例提交 MT5 reduce-only 平仓腿；paper 组走本地 Hyperliquid 撮合 + MT5 demo 完整模拟，live 组只有在 `auto_close_live_enabled=true` 且 `live_trading_enabled=true` 时才提交实盘平仓腿。
5. 如果统计样本不足导致退出线为 `0`，但当前平仓价差已经回到零轴以下且 `estimated_profit >= auto_close_min_profit`，仍允许自动平仓，避免老对冲组或新方向样本不足时错过已经明显盈利的退出。
6. 如果超过 `max_holding_minutes` 且利润达标，也允许按时间退出；该时间退出不再依赖统计退出线是否存在。

双边平仓都确认成交后，`realized_pnl` 优先使用真实平仓成交均价计算；自动平仓提交前的 `estimated_profit` 只作为无法取得 fill 均价时的兜底：

```text
long_hyperliquid_short_mt5:
真实平仓价差 = MT5 平仓买入成交均价 - HL 平仓卖出成交均价

long_mt5_short_hyperliquid:
真实平仓价差 = HL 平仓买入成交均价 - MT5 平仓卖出成交均价

realized_pnl = (真实开仓价差 - 真实平仓价差) * hyperliquid_quantity - fees - funding - swap
```

后台会按 `CARRY_COST_SYNC_INTERVAL_SECONDS` 周期同步真实/准真实持仓成本，并写入对冲组的 `funding` 和 `swap`：

- Hyperliquid live：调用官方 info endpoint 的 `userFunding`，按账户真实 funding 流水汇总。流水里的 `usdc` 为正表示收到资金费，系统会记为负成本；`usdc` 为负表示支付资金费，系统会记为正成本。
- Hyperliquid paper：不会产生真实交易所持仓，因此使用公开 `fundingHistory` 查询持仓期间每小时真实 funding rate，并按对冲组名义价值和 HL 多空方向模拟资金费。
- MT5 paper/live：从本机 MT5 终端读取当前持仓或历史成交里的 `swap` 字段。MT5 `swap` 为正表示收到，系统记为负成本；为负表示支付，系统记为正成本。

这些字段进入同一套 PnL 公式，因此持仓期间 `unrealized_pnl` 和最终 `realized_pnl` 会随真实资金费/过夜费变化。若同一 MT5 品种同方向存在多组同时持仓，当前版本会按匹配持仓总量比例分摊当前 position swap；需要逐单完全精确归属时，应继续接入 broker deal 的 position id 映射。

相关参数：

- `auto_close_enabled`：是否启用自动平仓调度。
- `auto_close_live_enabled`：是否允许自动平仓处理 live 对冲组，默认关闭；开启后仍需系统实盘总开关和平台发单开关。
- `exit_target_percentile`：退出线低分位数，默认 `0.25`，用于在同方向平仓价差分布中寻找更低的回落目标。
- 品种 `max_close_spread`：最大卖出/平仓价差，默认 `0` 表示不启用；启用后会收紧统计退出线，老对冲组缺少退出线时也会作为自动平仓兜底。
- `auto_close_unit_profit_buffer`：每份平仓利润缓冲，默认 `0`；该值与 `entry_spread`、`unit_cost` 同量纲，按每 1 份 Hyperliquid 数量的价差利润计算。该参数对 JP225、JPY、EUR 等价差量级差异很大的品种不通用，建议保持 `0`，用 `auto_close_min_profit` 控制总 USD 最小利润。
- `auto_close_min_profit`：自动平仓最低估算利润，默认 `0`。
- `max_holding_minutes`：最大持仓时间，超时且利润达标时允许退出。

自动平仓成功后，对冲组会写入 `close_reason`、`realized_pnl`，并记录两条反向订单和成交。live 自动平仓提交后可能先进入 `closing`，由 execution reconciler 回查确认最终成交。单腿平仓异常会进入 `manual_intervention`。

手工关闭对冲组会按 `execution_mode` 执行反向订单：`paper` 走本地 Hyperliquid 撮合 + MT5 demo 完整模拟，`live` 要求 `live_trading_enabled=true`。系统先提交 Hyperliquid 平仓腿，确认成交后再通过 MT5Adapter 按比例提交 MT5 平仓腿。平仓腿会带 `reduce_only` 语义；MT5Adapter 在 hedging 账户下会先用 `positions_get(symbol=...)` 找到被减仓方向的持仓 ticket，并在 `order_send` 请求里写入 `position`，找不到可平仓持仓或请求数量超过当前持仓数量时拒绝发单，避免反向单变成新增仓位。双边成交后状态变为 `closed`；Hyperliquid 只返回 `accepted/submitted` 且未成交时，状态保持 `closing`，不会把待成交订单当成已平仓；补腿失败或单边异常会进入 `manual_intervention`。

后台调度会运行 execution reconciler，周期性回查 `opening` / `closing` 对冲组里的 pending 订单。若只有 Hyperliquid 腿存在且回查确认已成交，reconciler 会按成交比例提交 MT5 后续腿：开仓补 MT5 开仓单，平仓补 MT5 reduce-only 平仓单；双边确认后，开仓推进到 `open`，平仓推进到 `closed` 并补写成交记录。回查发现已有双腿但单边成交和另一边失败时会进入 `manual_intervention`。reconciler 还会刷新 live positions：Hyperliquid 从 `clearinghouseState` 读取 perp 仓位，MT5 从终端 `positions_get()` 读取仓位，并校验已 `closed` 的 live 对冲组是否仍有对应符号残仓；发现残仓时会发告警并把对冲组拉回 `manual_intervention`。如果账户里存在无法匹配任何 live 对冲组的 Hyperliquid/MT5 仓位，系统会发出“外部孤儿仓位”告警，避免裸仓脱离对冲组管理。该回查只消费交易所/adapter 明确返回的订单和仓位状态，不用本地猜测成交。

管理员可手工触发 execution reconciler：对冲组页面的“同步执行状态”按钮会调用 `POST /api/execution/reconcile`，用于外部人工处理订单或仓位后立即刷新系统状态。

如果 readiness 或 reconciler 发现外部孤儿仓位，管理员可在仓位页点击“接管”，或调用 `POST /api/positions/{id}/adopt`。接管会按品种映射把单腿外部仓位创建为 `execution_mode=live`、`status=manual_intervention` 的对冲组，并记录 `adopted_external_position` 事件；该组不是完整双腿套利，只表示系统已经把外部仓位纳入生命周期。后续手工平仓只会对非零数量的平台腿提交 reduce-only 反向订单，不会为不存在的另一腿发单。

如果回查发现一条腿已经成交、另一条腿仍处于 `accepted/submitted/pending`，系统会先尝试撤销未成交腿。默认 `single_leg_action=manual_intervention` 时，对冲组进入 `manual_intervention` 等人工确认；如果品种映射配置为 `auto_close` 或 `reverse_filled_leg`，系统会对已成交腿提交一条反向市价冲销单。开仓阶段冲销成交后，对冲组标记为 `failed`，表示没有留下有效对冲组；平仓阶段冲销成交后，对冲组回到 `open`，表示原对冲关系已恢复。补偿单未成交或被拒绝时仍进入 `manual_intervention`。

reconciler 会读取账户级 `openOrders` / `userFills` 快照，用于恢复已有外部订单号的 pending 单；如果本地 Hyperliquid pending 单缺少 `external_order_id`，且账户快照中只有一条 symbol、side、quantity、时间都匹配的记录，也会补回外部订单号和成交明细。主动回查需要配置实际交易账户地址，API wallet/agent private key 场景应填写 `HYPERLIQUID_ACCOUNT_ADDRESS`。如果外部订单状态仍无法重建，reconciler 会先保留 pending 状态继续等待；超过 `execution_reconcile_pending_stale_seconds` 后仍无法确认时，会尝试撤销该订单并把对冲组升级为 `manual_intervention`。

实盘执行前可通过设置页或 `GET /api/settings/live-readiness` 做就绪检查。该检查不会发单，只验证全局 live 开关、Hyperliquid 账户回查地址和 `clearinghouseState` 只读连通性、MT5 Python 包和 `account_info()` 只读连通性、MT5 实盘开关、启用品种映射、MT5 合约规格、已同步 live 仓位归属以及单腿自动补偿配置。当前 Hyperliquid live 下单项固定为 block。如果 `positions` 表中存在未归属任何 live 对冲组的 Hyperliquid/MT5 仓位，或已关闭 live 对冲组仍有残余仓位，就绪检查会返回 `block`。`open_hedge_group()` 和 live `close_hedge_group()` 会在真实提交前执行同一套检查；存在 `block` 项时会直接拒绝 live 开仓或平仓。

## MT5 交易时段保护

系统在 MT5 官方 session / tick / trade_mode 判断前增加了一层本地交易时段模板，用于维护经纪商文档里的特殊规则，例如美股或类股票的盘前“仅限平仓”、指数的日内小休和仅报价窗口。设置页新增 `MT5 交易时段` tab，可对每个品种同步模板或手动维护 JSON 窗口。

本地模板的动作语义：

- `normal_trade`：允许新开仓和平仓。
- `reduce_only`：只允许平仓，禁止新增 MT5 仓位。
- `quote_only`：只允许报价，不允许开仓、改仓或平仓。
- `closed`：休市，禁止所有交易动作。

内置模板目前包括 `stock_us_close_only`、`index_us_jp`、`xauusd`、`energy`、`fx`、`crypto_major` 和 `always`。`SPCX`、常见美股代码会自动识别为 `stock_us_close_only`；`JP225`、`US30`、`USTEC`、`US500` 会自动识别为 `index_us_jp`。模板时区按 Exness 文档使用 `UTC`，启动时会先同步启用品种的模板，调度器随后按 `MT5_SESSION_TEMPLATE_REFRESH_HOURS` 周期重新同步。若某个品种文档未覆盖或 broker 临时调整，应在页面把模板改为 `manual_custom` 并关闭自动同步，直接维护 JSON 窗口。

## 下单前复核

候选机会不能直接下单，只有 `executable` 状态允许执行。执行引擎会再次读取严格同步报价：

- 两边报价时间差必须小于 `strict_quote_sync_ms`。
- 两边报价最大年龄必须小于 `quote_stale_ms`。
- 如果缓存同步失败，会主动刷新一次 Hyperliquid HTTP L2 和 MT5 tick，再重跑严格同步。
- 主动刷新后会用最新 bid/ask 重新计算当前方向价差；价差低于入场线或扣除保存的单位成本后净利润不足时，拒绝下单。
- 主动刷新成功且复核通过时，滑点风控使用 `DEFAULT_SLIPPAGE_BPS`，不再用刷新调用之间的本地时间差粗略折算。
- 同一个机会执行后会标记为 `executed`。
