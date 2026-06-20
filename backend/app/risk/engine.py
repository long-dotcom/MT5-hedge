from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.accounts.sync import latest_account_snapshots
from app.db.models import Alert, RiskEvent, RiskSetting


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""


def current_risk_setting(db: Session) -> RiskSetting:
    setting = db.query(RiskSetting).first()
    if not setting:
        setting = RiskSetting()
        db.add(setting)
        db.commit()
        db.refresh(setting)
    return setting


def pre_trade_check(db: Session, symbol: str, notional: float, slippage_bps: float, market_time: datetime, use_live_account_risk: bool = True) -> RiskDecision:
    setting = current_risk_setting(db)
    if setting.mode in {"paused", "emergency_stop", "reduce_only"}:
        return RiskDecision(False, f"当前风控模式为 {setting.mode}，禁止开新仓")
    if notional > setting.max_order_notional:
        return RiskDecision(False, f"单笔名义价值 {notional:.2f} USD 超过限制 {setting.max_order_notional:.2f} USD")
    if slippage_bps > setting.max_slippage_bps:
        return RiskDecision(False, "滑点超过限制")
    if use_live_account_risk:
        leverage = max(setting.new_order_leverage, 1.0)
        required_margin = notional / leverage
        accounts = latest_account_snapshots(db)
        if accounts:
            free_collateral = min((row.free_collateral or row.available_balance) for row in accounts)
            usable_margin = free_collateral * setting.max_new_margin_fraction
            if required_margin > usable_margin:
                return RiskDecision(False, f"新增保证金 {required_margin:.2f} 超过可用保证金折扣上限 {usable_margin:.2f}")
            weak_accounts = [row.platform for row in accounts if row.margin_ratio < setting.min_margin_ratio]
            if weak_accounts:
                return RiskDecision(False, f"账户保证金率低于阈值: {', '.join(weak_accounts)}")
    age = (datetime.utcnow() - market_time).total_seconds()
    if age > setting.max_market_age_seconds:
        return RiskDecision(False, "行情已过期")
    return RiskDecision(True)


def record_risk_event(db: Session, rule: str, message: str, symbol: str = "", level: str = "warning") -> None:
    db.add(RiskEvent(rule=rule, message=message, symbol=symbol, level=level))
    db.add(Alert(level=level, title=f"风控触发：{rule}", message=message))
    db.commit()
