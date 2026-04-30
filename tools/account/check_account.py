"""查詢當前幣安帳戶餘額、持倉、掛單"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.exchange import Exchange
from config.settings_loader import load_settings

def main():
    cfg = load_settings()
    ex = Exchange(cfg)
    ex.connect()

    print("\n===== 帳戶餘額 =====")
    try:
        bal = ex.exchange.fetch_balance()
        usdt = bal.get("USDT", {})
        total = bal.get("info", {}).get("totalWalletBalance", "?")
        avail = bal.get("info", {}).get("availableBalance", "?")
        print(f"總錢包餘額：{total} USDT")
        print(f"可用餘額：  {avail} USDT")
        print(f"free: {usdt.get('free')} | used: {usdt.get('used')} | total: {usdt.get('total')}")
    except Exception as e:
        print(f"查詢餘額失敗：{e}")

    print("\n===== 當前持倉 =====")
    try:
        positions = ex.exchange.fetch_positions(["BTC/USDT:USDT", "ETH/USDT:USDT"])
        has_position = False
        for p in positions:
            contracts = float(p.get("contracts") or 0)
            if contracts != 0:
                has_position = True
                print(f"[{p['symbol']}] {p['side']} | 張數={contracts} | "
                      f"開倉價={p.get('entryPrice')} | 標記價={p.get('markPrice')} | "
                      f"未實現損益={p.get('unrealizedPnl')} | 保證金={p.get('initialMargin')}")
        if not has_position:
            print("（無持倉）")
    except Exception as e:
        print(f"查詢持倉失敗：{e}")

    print("\n===== 掛單中 =====")
    try:
        for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT"]:
            orders = ex.exchange.fetch_open_orders(sym)
            for o in orders:
                print(f"[{o['symbol']}] {o['side']} {o['type']} | "
                      f"數量={o['amount']} | 價格={o.get('price')} | "
                      f"觸發={o.get('stopPrice')} | 狀態={o['status']}")
        print("（若上方無輸出代表無掛單）")
    except Exception as e:
        print(f"查詢掛單失敗：{e}")

    print("\n===== 近期成交（最近 10 筆） =====")
    try:
        for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT"]:
            trades = ex.exchange.fetch_my_trades(sym, limit=5)
            if trades:
                print(f"--- {sym} ---")
                for t in trades[-5:]:
                    print(f"  {t['datetime']} | {t['side']} | 價格={t['price']} | 量={t['amount']} | 手續費={t.get('fee')}")
    except Exception as e:
        print(f"查詢成交失敗：{e}")

if __name__ == "__main__":
    main()
