"""查詢幣安 BTC/ETH 永續的槓桿分層，確認實際可開最大倉位"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.exchange import Exchange
from config.settings_loader import load_settings

def main():
    cfg = load_settings()
    ex = Exchange(cfg)
    ex.connect()
    symbols = ["BTCUSDT", "ETHUSDT"]

    for sym in symbols:
        print(f"\n===== {sym} =====")
        try:
            result = ex.exchange.fapiPrivateGetLeverageBracket({"symbol": sym})
            brackets = result[0]["brackets"] if isinstance(result, list) else result["brackets"]
            print(f"{'Tier':<6}{'名義倉位上限(USDT)':<25}{'最大槓桿':<10}{'維持保證金率':<12}")
            for b in brackets:
                tier = b.get("bracket")
                cap = b.get("notionalCap")
                max_lev = b.get("initialLeverage")
                mmr = b.get("maintMarginRatio")
                print(f"{tier:<6}{cap:<25}{max_lev:<10}{mmr:<12}")
        except Exception as e:
            print(f"查詢失敗：{e}")

    lev = cfg.get("risk", {}).get("leverage")
    margin = cfg.get("trading", {}).get("position_fixed_usdt") or cfg.get("strategy", {}).get("position_fixed_usdt")
    print(f"\n===== 當前設定 =====")
    print(f"槓桿：{lev}x | 保證金：{margin} USDT | 名義倉位：{(lev or 0)*(margin or 0)} USDT")

if __name__ == "__main__":
    main()
