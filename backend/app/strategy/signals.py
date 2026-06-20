from dataclasses import dataclass


@dataclass
class SignalResult:
    status: str
    reason: str


def evaluate_signal(net_profit: float, annualized_return: float, min_net_profit: float, min_annualized_return: float) -> SignalResult:
    if net_profit <= 0:
        return SignalResult("rejected", "扣除成本后无利润")
    if net_profit < min_net_profit:
        return SignalResult("candidate", "净利润未达到执行阈值")
    if annualized_return < min_annualized_return:
        return SignalResult("candidate", "年化收益未达到执行阈值")
    return SignalResult("executable", "")

