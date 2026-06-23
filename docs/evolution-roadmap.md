# 演进路线

当前系统定位是“实时行情 + 策略研究 + Paper 自动交易验证 + 受保护 MT5 实盘执行接入”。Hyperliquid paper 已回到本地 `QuoteCache` 撮合，Hyperliquid live 当前只保留账户、仓位和订单快照的只读查询，真实下单 SDK 暂未启用。

## 当前边界

- 行情：Hyperliquid 使用原生 WebSocket/HTTP `l2Book`，MT5 使用本机终端 Python API 轮询 tick。
- 执行：`ExecutionGateway` 统一输出 `OrderEvent` / `FillEvent`，`build_execution_gateway()` 当前统一返回 `AdapterExecutionGateway`。
- Paper：Hyperliquid 腿按最新 bid/ask 本地撮合，MT5 腿走 demo `order_send`。
- Live：MT5 真实下单受 `LIVE_TRADING_ENABLED` 和 `MT5_LIVE_ORDER_ENABLED` 双重保护；Hyperliquid live 下单固定 block。
- 回查：`execution_reconciler` 负责 pending 订单、live 仓位、closed 残仓和外部孤儿仓位同步。

## 优先级

1. 稳定本地撮合：补齐 market、limit、post-only、reduce-only、TTL、部分成交和滑点模型。
2. 强化执行编排：继续保持“Hyperliquid 先成交、MT5 后补腿”的顺序，失败时进入人工处理或按品种配置反向冲销。
3. 完善恢复能力：启动后重建 pending 订单、账户快照、缺失外部单号和成交明细。
4. 扩展风控：对单品种、全局敞口、MT5 会话、异常延迟和报价质量继续加保护。
5. 评估 Hyperliquid live SDK：在 paper 撮合稳定后，再单独设计真实下单接入，不影响当前对冲组和本地撮合逻辑。

## 非目标

- 不把双腿对冲组交给第三方框架表达。
- 不让任何外部 SDK 绕过本系统的风控、readiness 和对冲组生命周期。
- 不在 Hyperliquid live 下单未验证前开放真实提交。
