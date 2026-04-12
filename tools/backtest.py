"""
TITAN v1 — 策略回測腳本
時間範圍：2025/7 ~ 2026/4
標的：BTC/USDT, ETH/USDT

策略規則：
  - 進場：1H MACD 柱狀圖交叉觸發（金叉多 / 死叉空）
  - 止損 Stage 1：進場價 ±3% → 減倉一半
  - 止損 Final  ：箱體邊界 × (1 ± 1%) → 剩餘全部平倉
  - TP1          ：4H MACD 反向交叉 → 平倉一半，止損移至開倉價（BE）
  - TP2          ：BE 後下一次 4H MACD 同向訊號消失 → 平倉剩餘
  - 每筆固定 100 USDT 保證金，100x 槓桿
"""

import ccxt
import pandas as pd
import numpy as np
from datetime import timezone

# ── 工具函數 ────────────────────────────────────────────────────────────

def calc_macd_hist(close: pd.Series, fast=12, slow=26, sig=9) -> pd.Series:
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    macd  = ema_f - ema_s
    signal = macd.ewm(span=sig, adjust=False).mean()
    return macd - signal

def calc_pnl(side, entry, exit_p, notional, fee=0.0005):
    """回傳已扣手續費的 USDT 損益"""
    if side == 'LONG':
        raw = (exit_p - entry) / entry * notional
    else:
        raw = (entry - exit_p) / entry * notional
    return raw - notional * fee * 2

def fetch(ex, sym, tf, limit):
    raw = ex.fetch_ohlcv(sym, timeframe=tf, limit=limit)
    df  = pd.DataFrame(raw, columns=['ts','open','high','low','close','volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    return df.astype({'open':float,'high':float,'low':float,'close':float,'volume':float})

# ── 箱體偵測（每日預算）──────────────────────────────────────────────────

def build_daily_boxes(df_1d,
                      form_days=5,
                      anchor_window=30,
                      breakdown_vol_min=1.5):
    """
    預先為每一天計算「當天有效的箱體（lower, upper）」。
    回傳 dict: { date → (lower, upper) }
    """
    df = df_1d.reset_index(drop=True).copy()
    n  = len(df)
    avg_vol = df['volume'].mean()

    # 先找出所有箱體的起訖（跟視覺化工具相同邏輯）
    boxes = []
    search = 0
    while search < n - form_days - 2:
        win_end    = min(search + anchor_window, n)
        anchor_idx = int(df['volume'].iloc[search:win_end].idxmax())
        form_end   = min(anchor_idx + form_days, n - 1)
        box_lower  = float(df['low'].iloc[anchor_idx:form_end+1].min())
        box_upper  = float(df['high'].iloc[anchor_idx:form_end+1].max())
        end_idx    = form_end
        invalidated = False; inv_idx = None

        for i in range(form_end + 1, n):
            h = float(df['high'].iloc[i])
            c = float(df['close'].iloc[i])
            v = float(df['volume'].iloc[i])
            if h > box_upper:
                box_upper = h
            end_idx = i
            if c < box_lower and v >= avg_vol * breakdown_vol_min:
                invalidated = True; inv_idx = i; break

        if (end_idx - anchor_idx) < 7:
            search = anchor_idx + 1; continue

        boxes.append({'start': anchor_idx, 'end': end_idx,
                      'lower': box_lower,  'upper': box_upper,
                      'invalidated': invalidated})
        if invalidated and inv_idx:
            search = inv_idx
        else:
            break

    # 建立 date → (lower, upper) 映射
    day_box = {}
    for b in boxes:
        for idx in range(b['start'], b['end'] + 1):
            d = df['ts'].iloc[idx].date()
            # 使用 running upper（模擬當天的實際上緣）
            running_upper = float(df['high'].iloc[b['start']:idx+1].max())
            day_box[d] = (b['lower'], running_upper)

    return day_box


# ── 單一標的回測 ──────────────────────────────────────────────────────────

def backtest_symbol(symbol, df_1h, df_4h, day_box):
    NOTIONAL  = 10_000.0   # 100 USDT × 100x
    FEE       = 0.0005
    SL1_PCT   = 0.03       # 第一階段減倉觸發 3%
    BOX_BUF   = 0.01       # 箱體邊界外 1% 才完全認賠

    # 計算 MACD
    df_1h = df_1h.copy()
    df_4h = df_4h.copy()
    df_1h['hist'] = calc_macd_hist(df_1h['close'])
    df_4h['hist'] = calc_macd_hist(df_4h['close'])

    # 4H MACD 反向訊號：對每根 1H K 棒查詢當時最新 4H MACD
    df_4h = df_4h.set_index('ts').sort_index()

    trades    = []
    pos       = None   # 持倉狀態 dict

    for i in range(34, len(df_1h)):   # 跳過 MACD 暖身期
        row   = df_1h.iloc[i]
        ts    = row['ts']
        date  = ts.date()
        hi    = float(row['high'])
        lo    = float(row['low'])
        cl    = float(row['close'])

        # 取得當天箱體
        box = day_box.get(date)
        if box is None:
            continue
        box_lower, box_upper = box

        # 1H MACD 交叉
        hist_now  = float(df_1h['hist'].iloc[i])
        hist_prev = float(df_1h['hist'].iloc[i-1])
        sig_long  = hist_now > 0 and hist_prev <= 0
        sig_short = hist_now < 0 and hist_prev >= 0

        # 4H 最新 hist（取 ts 之前最後一根 4H）
        h4_sub = df_4h[df_4h.index <= ts]['hist']
        h4_now  = float(h4_sub.iloc[-1]) if len(h4_sub) >= 2 else 0.0
        h4_prev = float(h4_sub.iloc[-2]) if len(h4_sub) >= 2 else 0.0
        tp1_long_trigger  = h4_now < 0 and h4_prev >= 0   # 4H 死叉 → 多單 TP1
        tp1_short_trigger = h4_now > 0 and h4_prev <= 0   # 4H 金叉 → 空單 TP1

        # ── 管理現有持倉 ──────────────────────────────────────────────
        if pos is not None:
            side       = pos['side']
            entry_p    = pos['entry_p']
            stage      = pos['stage']    # 'full' | 'half' | 'quarter'
            sl1        = pos['sl1']
            sl_final   = pos['sl_final']
            be_stop    = pos.get('be_stop')
            tp1_done   = pos.get('tp1_done', False)

            # 計算當前持倉名義
            def cur_notional():
                return {'full': NOTIONAL, 'half': NOTIONAL/2, 'quarter': NOTIONAL/4}[stage]

            exited = False

            # 1. TP1：4H MACD 反向（還沒做過）
            if not tp1_done:
                tp1_hit = (side == 'LONG' and tp1_long_trigger) or \
                          (side == 'SHORT' and tp1_short_trigger)
                if tp1_hit:
                    tp_notional = cur_notional() / 2
                    pnl = calc_pnl(side, entry_p, cl, tp_notional, FEE)
                    trades.append({'ts': ts, 'sym': symbol, 'side': side,
                                   'entry': entry_p, 'exit': cl, 'type': 'TP1',
                                   'notional': tp_notional, 'pnl': round(pnl, 2)})
                    pos['stage']    = 'half' if stage == 'full' else 'quarter'
                    pos['tp1_done'] = True
                    pos['be_stop']  = entry_p

            if not exited:
                stage     = pos['stage']
                be_stop   = pos.get('be_stop')

                # 2. Stage 1 SL（3% 減倉，僅限 full stage）
                if stage == 'full':
                    sl1_hit = (side == 'LONG' and lo <= sl1) or \
                              (side == 'SHORT' and hi >= sl1)
                    if sl1_hit:
                        exit_p  = sl1
                        pnl     = calc_pnl(side, entry_p, exit_p, NOTIONAL/2, FEE)
                        trades.append({'ts': ts, 'sym': symbol, 'side': side,
                                       'entry': entry_p, 'exit': exit_p, 'type': 'SL1',
                                       'notional': NOTIONAL/2, 'pnl': round(pnl, 2)})
                        pos['stage'] = 'half'
                        stage = 'half'

                # 3. BE stop（TP1 後止損移至開倉價）
                if be_stop is not None and not exited:
                    be_hit = (side == 'LONG' and lo <= be_stop) or \
                             (side == 'SHORT' and hi >= be_stop)
                    if be_hit:
                        pnl = calc_pnl(side, entry_p, be_stop, cur_notional(), FEE)
                        trades.append({'ts': ts, 'sym': symbol, 'side': side,
                                       'entry': entry_p, 'exit': be_stop, 'type': 'BE',
                                       'notional': cur_notional(), 'pnl': round(pnl, 2)})
                        pos = None; exited = True

                # 4. Final SL（箱體邊界外 1%）
                if not exited:
                    final_hit = (side == 'LONG' and lo <= sl_final) or \
                                (side == 'SHORT' and hi >= sl_final)
                    if final_hit:
                        pnl = calc_pnl(side, entry_p, sl_final, cur_notional(), FEE)
                        trades.append({'ts': ts, 'sym': symbol, 'side': side,
                                       'entry': entry_p, 'exit': sl_final, 'type': 'SL_FINAL',
                                       'notional': cur_notional(), 'pnl': round(pnl, 2)})
                        pos = None; exited = True

                # 5. TP2：TP1 後，1H MACD 再次反向 → 全平
                if not exited and tp1_done:
                    tp2_hit = (side == 'LONG' and sig_short) or \
                              (side == 'SHORT' and sig_long)
                    if tp2_hit:
                        pnl = calc_pnl(side, entry_p, cl, cur_notional(), FEE)
                        trades.append({'ts': ts, 'sym': symbol, 'side': side,
                                       'entry': entry_p, 'exit': cl, 'type': 'TP2',
                                       'notional': cur_notional(), 'pnl': round(pnl, 2)})
                        pos = None; exited = True

        # ── 開新倉（無持倉時）────────────────────────────────────────
        if pos is None:
            if sig_long:
                pos = {
                    'side': 'LONG', 'entry_p': cl,
                    'stage': 'full',
                    'sl1':      cl * (1 - SL1_PCT),
                    'sl_final': box_lower * (1 - BOX_BUF),
                    'tp1_done': False, 'be_stop': None,
                }
            elif sig_short:
                pos = {
                    'side': 'SHORT', 'entry_p': cl,
                    'stage': 'full',
                    'sl1':      cl * (1 + SL1_PCT),
                    'sl_final': box_upper * (1 + BOX_BUF),
                    'tp1_done': False, 'be_stop': None,
                }

    # 強制平倉最後一筆
    if pos is not None:
        last = df_1h.iloc[-1]
        def cur_notional():
            return {'full': NOTIONAL, 'half': NOTIONAL/2, 'quarter': NOTIONAL/4}[pos['stage']]
        pnl = calc_pnl(pos['side'], pos['entry_p'], float(last['close']), cur_notional(), FEE)
        trades.append({'ts': last['ts'], 'sym': symbol, 'side': pos['side'],
                       'entry': pos['entry_p'], 'exit': float(last['close']),
                       'type': 'EOD', 'notional': cur_notional(), 'pnl': round(pnl, 2)})

    return trades


# ── 主程式 ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    ex = ccxt.binance({'options': {'defaultType': 'future'}})

    SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT']
    all_trades = []

    for sym in SYMBOLS:
        short = sym.split('/')[0]
        print(f'\n[{short}] 抓取資料中...')

        df_1h = fetch(ex, sym, '1h', 270*24)
        df_4h = fetch(ex, sym, '4h', 270*6)
        df_1d = fetch(ex, sym, '1d', 270)

        print(f'[{short}] 1H:{len(df_1h)} 4H:{len(df_4h)} 1D:{len(df_1d)} | '
              f'{df_1d["ts"].iloc[0].date()} ~ {df_1d["ts"].iloc[-1].date()}')

        day_box = build_daily_boxes(df_1d)
        trades  = backtest_symbol(short, df_1h, df_4h, day_box)
        all_trades.extend(trades)
        print(f'[{short}] 產生 {len(trades)} 筆交易記錄')

    # ── 彙整結果 ──────────────────────────────────────────────────────
    df_t = pd.DataFrame(all_trades)
    if df_t.empty:
        print('\n無任何交易記錄')
    else:
        df_t['ts'] = pd.to_datetime(df_t['ts'])
        df_t = df_t.sort_values('ts').reset_index(drop=True)

        total_pnl = df_t['pnl'].sum()
        wins      = df_t[df_t['pnl'] > 0]
        losses    = df_t[df_t['pnl'] <= 0]

        # 每筆完整交易（把同一個 entry_p 的分批出場加總）
        df_t['trade_id'] = (df_t['entry'] != df_t['entry'].shift()).cumsum()
        trade_pnl = df_t.groupby(['sym','trade_id','side','entry'])['pnl'].sum().reset_index()
        trade_pnl['win'] = trade_pnl['pnl'] > 0

        win_trades  = trade_pnl[trade_pnl['win']]
        loss_trades = trade_pnl[~trade_pnl['win']]

        print('\n' + '='*60)
        print('TITAN v1 回測結果（2025/07 ~ 2026/04）')
        print('='*60)
        print(f'  交易標的     : BTC / ETH')
        print(f'  每筆保證金   : 100 USDT × 100x 槓桿')
        print(f'  總 P&L       : {total_pnl:+.2f} USDT')
        print()
        print(f'  完整交易筆數 : {len(trade_pnl)}')
        print(f'  勝率         : {len(win_trades)/len(trade_pnl)*100:.1f}%  '
              f'({len(win_trades)} 勝 / {len(loss_trades)} 敗)')
        print(f'  平均獲利     : {win_trades["pnl"].mean():+.2f} USDT' if len(win_trades) else '  平均獲利     : --')
        print(f'  平均虧損     : {loss_trades["pnl"].mean():+.2f} USDT' if len(loss_trades) else '  平均虧損     : --')
        print(f'  最佳單筆     : {trade_pnl["pnl"].max():+.2f} USDT')
        print(f'  最差單筆     : {trade_pnl["pnl"].min():+.2f} USDT')
        print()

        # 分標的
        for sym in ['BTC', 'ETH']:
            sub = trade_pnl[trade_pnl['sym'] == sym]
            if sub.empty: continue
            sw = sub[sub['win']]; sl = sub[~sub['win']]
            print(f'  [{sym}] {len(sub)} 筆 | P&L {sub["pnl"].sum():+.2f} | '
                  f'勝率 {len(sw)/len(sub)*100:.0f}%')

        print()

        # 分出場類型
        print('  各出場類型明細：')
        for t, g in df_t.groupby('type'):
            print(f'    {t:10s}: {len(g):3d} 筆 | 合計 {g["pnl"].sum():+8.2f} USDT')

        print()

        # 前 10 筆交易明細
        print('  最近 10 筆明細：')
        for _, r in df_t.tail(10).iterrows():
            print(f'    {str(r["ts"])[:16]}  {r["sym"]:3s} {r["side"]:5s} '
                  f'{r["type"]:9s}  entry={r["entry"]:>9.2f}  exit={r["exit"]:>9.2f}  '
                  f'pnl={r["pnl"]:+8.2f}')
        print('='*60)
