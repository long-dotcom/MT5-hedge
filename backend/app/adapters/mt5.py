from app.adapters.base import AdapterOrder, AdapterOrderResult
from app.adapters.paper import PaperAdapter
from app.config.settings import get_settings


class MT5Adapter(PaperAdapter):
    def __init__(self, live: bool = False) -> None:
        super().__init__("mt5", price_bias_bps=20.0)
        self.live = live
        self.settings = get_settings()

    def place_order(self, order: AdapterOrder) -> AdapterOrderResult:
        # 中文注释：MT5 实盘需要本机终端登录成功后再接入真实下单。
        if not self.live:
            return super().place_order(order)
        if not self.settings.mt5_login or not self.settings.mt5_password or not self.settings.mt5_server:
            return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, "MT5 凭证未配置")
        return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, "首版未启用 MT5 真实下单调用")
