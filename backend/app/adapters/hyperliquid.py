from app.adapters.base import AdapterOrder, AdapterOrderResult
from app.adapters.paper import PaperAdapter
from app.config.settings import get_settings


class HyperliquidAdapter(PaperAdapter):
    def __init__(self, live: bool = False) -> None:
        super().__init__("hyperliquid", price_bias_bps=-20.0)
        self.live = live
        self.settings = get_settings()

    def place_order(self, order: AdapterOrder) -> AdapterOrderResult:
        # 中文注释：实盘默认关闭，避免误触发真实资金交易。
        if not self.live:
            return super().place_order(order)
        if not self.settings.hyperliquid_private_key or not self.settings.hyperliquid_wallet_address:
            return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, "Hyperliquid 凭证未配置")
        return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, "首版未启用 Hyperliquid 真实下单 SDK 调用")
