# 风控文档

风控模块在开仓前执行预检查，并在异常时写入风控事件和站内告警。

## 系统模式

- `normal`：允许开仓和平仓。
- `reduce_only`：只允许平仓，禁止开新仓。
- `paused`：暂停策略开仓。
- `emergency_stop`：紧急停止全部自动交易。

## 开仓前检查

当前首版检查：

- 系统模式是否允许开仓。
- 单笔名义价值是否超过 `max_order_notional`。
- 预估滑点是否超过 `max_slippage_bps`。
- 行情时间是否超过 `max_market_age_seconds`。
- 执行前严格报价同步是否通过。

## 行情同步风控

系统不允许直接拿两个平台“最新价”相减后下单。开仓前必须通过严格同步报价：

- `strict_quote_sync_ms`：两边报价本机接收时间允许的最大差值。
- `quote_stale_ms`：任一平台报价距离当前时间允许的最大年龄。
- `loose_quote_sync_ms`：宽松扫描使用的时间窗口，只用于候选发现。

如果 Hyperliquid 已更新而 MT5 未更新，或 MT5 tick 先到而 Hyperliquid 旧价未刷新，严格检查会拒绝交易，避免假价差触发。

自动纸面执行不会绕过这些检查。自动执行器只负责在后端发现持续满足条件的 `executable` 机会，并在确认次数、持续时间、冷却和未平对冲组上限都满足后调用同一套开仓接口；开仓前仍会重新执行严格行情同步和资金风控。

预留但需要真实账户数据增强的检查：

- 单品种敞口。
- 总杠杆。
- 保证金率。
- API 错误次数。
- 强平价距离。

## 成本数据来源

成本模型会影响风控和信号阈值：

- Hyperliquid fee 从 `userFees` 读取账户基础费率；HIP-3/XYZ 品种会结合 `metaAndAssetCtxs` 元数据自动修正为对应 growth/standard 有效费率。
- Hyperliquid funding 从公开市场上下文读取。
- MT5 swap 当前按 `MT5_SWAP_FREE=true` 不计；关闭免隔夜后再从 `symbol_info()` 读取估算。
- MT5 commission 当前按账户规则设为 0。

## 新增仓位资金口径

下单前风控不使用账户总余额直接判断，而使用最新账户快照中的 `free_collateral`：

- Hyperliquid：读取 perp `clearinghouseState` 和 spot `spotClearinghouseState`，展示 Perp 权益、Spot USDC、Spot 锁定、可提取和可用保证金。
- MT5：使用 `account_info()` 的 `margin_free` 作为可用保证金。
- 新订单估算保证金 = `notional / new_order_leverage`。
- 默认单笔最多使用 `free_collateral * max_new_margin_fraction`，当前默认 30%。

该口径偏保守，用于避免把支撑现有仓位的保证金误当作可自由使用余额。
- MT5 spread rebate 当前按 `MT5_SPREAD_REBATE_RATE=0.20` 抵扣点差成本。

Paper 模式默认不使用真实账户资金快照做保证金阻断，便于模拟自动执行和延迟成交；仍会检查系统模式、单笔名义价值、滑点、行情同步和行情过期。需要让 paper 也按真实账户可用保证金约束时，可在策略设置中开启 `paper_use_live_account_risk`。Live 模式始终强制使用真实账户资金风控。

## Maker 执行风险

Hyperliquid maker 能降低手续费，但会带来成交不确定性：

- HL maker 未成交时不能先打 MT5，否则会裸露单边风险。
- HL 部分成交时只能按成交数量去 MT5 对冲。
- HL 成交后必须重新校验 MT5 最新报价。
- 任一边失败时默认进入 `manual_intervention`。

## 实盘保护

实盘默认关闭。开启实盘必须满足：

1. 管理员登录。
2. 设置执行模式为 `live`。
3. 在实盘开关页面输入 `ENABLE LIVE TRADING`。
4. `.env` 中配置 Hyperliquid 和 MT5 凭证。

首版任一边下单失败时，不自动补仓，直接把对冲组标记为 `manual_intervention` 并产生告警。
