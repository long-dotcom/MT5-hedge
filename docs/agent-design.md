# Agent 接入功能设计

本文档描述后续 Agent 辅助层的功能设计。当前只作为演进设计，不代表代码已实现。

## 1. 目标定位

Agent 的定位是“交易运维助手”和“异常再平衡建议器”，不是绕过系统的自动交易机器人。

它可以定时读取系统已有状态，解释当前异常，生成处理建议，并在高安全条件满足后调用系统接口提交人工确认后的动作。所有下单、撤单、补腿、平仓和接管动作仍必须经过现有风控、readiness、执行网关、reconciler、对冲组生命周期和审计链路。

## 2. 核心场景

### 2.1 只读诊断

- 定时读取价差研究结果、报价时差、资金费趋势、链路监控和当前行情同步状态。
- 定时读取账户、仓位、对冲组、订单、成交、日志、告警、风控状态和 readiness 状态。
- 识别系统是否存在扫描停滞、报价过旧、SSE 断开、后台任务失败、数据库延迟、MT5 会话关闭等运行异常。
- 汇总“当前是否适合开仓、平仓、补腿、接管”的判断理由。

### 2.2 对冲组异常识别

- 单腿持仓：系统对冲组只有一边成交或外部仓位只剩一边。
- 双腿数量不平衡：HL 与 MT5 数量按合约规格换算后偏差超过阈值。
- 方向不一致：持仓方向与对冲组方向不匹配。
- closed 残仓：对冲组已关闭但 live positions 仍显示残余仓位。
- pending 长时间未恢复：订单长时间 pending、缺失 external order id、成交明细不完整。
- 外部孤儿仓位：不属于任何系统对冲组的 live position。
- 风控冲突：补腿会扩大敞口、超出保证金、触发强平距离或超过平台敞口限制。

### 2.3 建议生成

Agent 为每个异常生成结构化建议：

- 异常类型、影响平台、关联对冲组、关联订单和仓位。
- 建议动作：观察、同步、撤单、接管、手动平仓、补残腿、标记人工处理。
- 建议补哪一腿、方向、数量、订单类型、价格来源、最大滑点、TTL、reduce-only/post-only 语义。
- 不建议处理的原因，例如报价不同步、MT5 休市、readiness 未通过、未知外部仓位、账户权限不足。
- 风险等级和证据引用，包括接口快照时间、行情时间、订单状态、仓位数量和日志片段。

### 2.4 受控补残腿

补残腿属于高风险动作，默认只生成建议，不自动提交。

进入受控自动补残腿评估前必须满足：

- 异常归属明确，只能处理已归属系统对冲组的残腿或不平衡。
- 不处理未知外部孤儿仓位；这类仓位只能建议人工接管。
- 通过 `risk`、`readiness`、MT5 会话权限、平台交易开关、全局 live 开关和管理员权限。
- 执行前重新做严格行情同步复核，报价过旧或双边时间差超限则拒绝。
- 订单不会扩大净风险，优先使用 reduce-only 或不会增加裸露方向的订单。
- 补腿数量经过品种映射、MT5 合约规格、最小手数、步进、最小名义额和最大滑点校验。
- 每次动作必须可审计，可回放，可由 reconciler 后续恢复。

## 3. 接入架构

建议使用 LangChain 生态，但更适合采用 LangGraph 编排 Agent 状态机：

- LangChain：封装工具调用、提示模板、结构化输出和模型适配。
- LangGraph：编排“读取状态 -> 诊断 -> 风控复核 -> 建议生成 -> 人工确认 -> 调用动作接口 -> 复盘”的有状态流程。
- LLM 只做诊断解释和建议编排，不直接拥有平台 SDK 凭证。
- 所有工具都封装成系统内部 API 调用，工具层只暴露受控能力。

推荐结构：

```text
Agent Scheduler
  -> Agent Runtime (LangGraph)
     -> Read Tools
        -> dashboard summary / equity curve
        -> pipeline diagnostics
        -> spread analytics
        -> funding analytics
        -> lead-lag report
        -> accounts / positions
        -> hedge groups / orders / fills
        -> logs / alerts / risk / readiness
     -> Diagnosis Node
     -> Safety Gate Node
     -> Recommendation Node
     -> Human Approval Node
     -> Action Tool Node
        -> existing manual close / adopt / reconcile / settings-safe APIs
        -> future rebalance API
     -> Audit Logger
```

## 4. 可调用系统能力

### 4.1 只读工具

优先接入已有 API：

- `/api/dashboard/summary`
- `/api/dashboard/equity-curve`
- `/api/diagnostics/pipeline`
- `/api/analytics/spread-summary`
- `/api/analytics/spread-series`
- `/api/analytics/funding-series`
- `/api/analytics/lead-lag`
- `/api/accounts`
- `/api/accounts/snapshots`
- `/api/positions`
- `/api/hedge-groups`
- `/api/orders`
- `/api/fills`
- `/api/logs`
- `/api/alerts`
- `/api/dashboard/risk-summary`
- readiness 相关接口，后续如当前没有完整 HTTP 读口，可补只读端点。

### 4.2 建议工具

后续可新增内部接口：

- `POST /api/agent/diagnose`：手动触发一次 Agent 诊断。
- `GET /api/agent/recommendations`：查看建议列表。
- `GET /api/agent/recommendations/{id}`：查看建议证据、风控结果和可执行性。
- `POST /api/agent/recommendations/{id}/ack`：管理员确认已读。
- `POST /api/agent/recommendations/{id}/dismiss`：管理员忽略并填写原因。

### 4.3 动作工具

动作工具只调用系统接口，不直接调用交易所或 MT5 SDK：

- 手动平仓：复用现有对冲组平仓能力。
- 仓位接管：复用现有外部仓位接管流程。
- 执行回查：触发 reconciler 或读取回查结果。
- 撤销异常 pending：必须走执行网关和订单状态机。
- 补残腿：建议新增独立 `rebalance` 接口，由后端做全部安全校验。

补残腿接口建议形态：

```text
POST /api/agent/rebalance-proposals/{id}/execute
```

执行前后端必须重新计算建议，不信任前端或 LLM 传入的价格和数量。LLM 传入的内容只能作为“建议说明”，不能作为最终下单参数来源。

## 5. 安全边界

### 5.1 绝对禁止

- Agent 直接读取或持有交易所私钥、MT5 密码。
- Agent 直接调用 Hyperliquid SDK、MT5 Python API 或任何平台下单接口。
- Agent 绕过 `ExecutionGateway`、`risk`、`readiness`、reconciler、审计和管理员权限。
- Agent 自动接管未知外部孤儿仓位。
- Agent 在报价不同步、MT5 休市、readiness 阻塞、风险模式非 normal 时自动补腿。
- Agent 使用 LLM 自己推导的价格作为最终下单价。

### 5.2 高风险动作确认

以下动作必须二次确认：

- live 或 paper-live 环境的补残腿。
- 任何可能扩大平台净敞口的动作。
- 接管外部仓位。
- 强制撤 pending。
- 手动关闭或恢复异常对冲组。

确认信息至少包括：

- 操作人、时间、来源 IP。
- 建议 ID、对冲组 ID、订单 ID、仓位快照 ID。
- 执行动作、平台、品种、方向、数量、订单类型、限价和 TTL。
- readiness 结果、风控结果、行情快照时间和最大滑点。
- 成功、失败、撤销、部分成交和后续 reconciler 结果。

## 6. 前端呈现

建议做成“页内悬浮助手 + 独立 Agent 中心”。

### 6.1 悬浮助手

位置类似客服悬浮窗，但视觉上更像系统运维助手：

- 固定在右下角，可折叠。
- 显示 Agent 状态：空闲、诊断中、有建议、高风险待确认、执行中、异常。
- 支持按当前页面给出上下文建议，例如在仓位页优先解释残腿，在链路页优先解释报价阻塞。
- 不自动弹出遮挡主要操作，高风险建议只显示角标和页头提示。
- 点击后打开建议抽屉，展示证据、建议动作、风险拦截原因和可执行按钮。

### 6.2 Agent 中心

新增独立页面或日志中心子页：

- 建议列表：按风险等级、状态、品种、平台、对冲组筛选。
- 诊断时间线：展示 Agent 每次定时诊断结果。
- 待确认动作：集中展示需要管理员处理的高风险建议。
- 历史复盘：查看建议、确认、执行、失败和 reconciler 恢复链路。
- 配置页入口：诊断周期、只读/建议/人工确认执行模式、最大建议数量、静默时间窗口。

### 6.3 页面联动

- 仪表盘：展示 Agent 总状态、最新高风险建议和系统健康摘要。
- 链路监控：解释阻塞环节和最近一次恢复建议。
- 价差研究：解释当前价差分布是否支持入场/平仓，不直接给下单按钮。
- 资金费研究：提示资金费方向、累计成本和持仓时间风险。
- 对冲组：展示每个对冲组的 Agent 诊断标签。
- 仓位：突出单腿、残仓、孤儿仓位和接管建议。
- 执行记录：解释 pending、部分成交和失败订单的后续建议。
- 风控：展示 Agent 被哪些风控条件阻止。
- 日志：将 Agent 建议、确认、执行结果作为可筛选事件。

## 7. 运行模式

### 7.1 observe

只读观察模式。只生成诊断事件，不生成可执行建议。

### 7.2 advise

建议模式。生成结构化建议，但所有动作都需要人工从页面确认。

### 7.3 guarded_execute

受保护执行模式。仅允许执行低风险、已归属、不会扩大敞口的补残腿动作；每次动作仍需要后端重新风控和审计。此模式不作为首版目标。

## 8. 数据结构草案

```text
AgentRecommendation
  id
  status: open | acknowledged | dismissed | approved | executing | executed | failed | blocked
  severity: info | warning | critical
  category: stale_quote | single_leg | imbalance | orphan_position | pending_stuck | risk_blocked
  symbol
  platform
  hedge_group_id
  evidence
  recommendation
  safety_checks
  proposed_action
  created_at
  updated_at
```

```text
AgentActionProposal
  recommendation_id
  action_type: observe | sync | reconcile | cancel_order | adopt_position | close_group | rebalance_leg
  symbol
  platform
  side
  quantity
  order_type
  price_reference
  max_slippage_bps
  ttl_seconds
  reduce_only
  post_only
  blocked_reasons
```

## 9. 分阶段落地

### A1：只读 Agent

- 新增 Agent 中心页面和建议列表。
- Agent 定时读取只读 API。
- 生成诊断和建议，不提供执行按钮。
- 所有建议写入日志和审计。

### A2：人工确认动作

- 支持从建议调用现有手工平仓、接管、回查接口。
- 所有动作仍由管理员点击确认。
- 前端展示执行前后差异和失败原因。

### A3：补残腿建议

- 新增 rebalance proposal 后端计算接口。
- Agent 只提交“需要计算补腿方案”的请求。
- 后端返回可执行性、阻塞原因和最终参数。

### A4：受保护自动补残腿

- 仅限已归属系统对冲组。
- 仅限低风险不扩大敞口动作。
- 需要完整测试、演练、审计和回滚策略。

## 10. 非目标

- 不用 Agent 替代风控。
- 不用 Agent 替代 reconciler。
- 不让 Agent 自行决定真实下单参数。
- 不让 Agent 自动处理未知外部仓位。
- 不在 Hyperliquid live 完整开放前通过 Agent 变相开放 live 下单。
