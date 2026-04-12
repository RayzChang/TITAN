"""
TITAN — 箱體視覺化工具
直接從幣安抓日線資料，套入 Volume-Anchored Box Detection 算法後畫圖。

執行：
    python tools/visualize_box.py [--symbol ETHUSDT] [--days 180]
"""

import sys
import argparse
import ccxt
import pandas as pd
import mplfinance as mpf
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt

# ── 參數 ────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument('--symbol', default='ETHUSDT', help='Binance symbol (default: ETHUSDT)')
parser.add_argument('--days',   default=180, type=int, help='日線根數 (default: 180)')
args = parser.parse_args()

SYMBOL    = args.symbol
DAYS      = args.days
BOX_LOOKBACK = 120   # 與 settings.yaml 一致

# ── 抓資料（公開 API，不需 key）─────────────────────────────────────────

print(f"正在抓 {SYMBOL} 日線 {DAYS} 根...")
ex = ccxt.binance({'options': {'defaultType': 'future'}})
ohlcv = ex.fetch_ohlcv(f"{SYMBOL[:3]}/USDT:USDT", timeframe='1d', limit=DAYS)

df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
df.set_index('timestamp', inplace=True)
df = df.astype(float)

print(f"資料範圍：{df.index[0].date()} ~ {df.index[-1].date()}（共 {len(df)} 根）")

# ── 箱體偵測（與 range_breakout.py 完全一致的邏輯）──────────────────────

def detect_box(df_1d: pd.DataFrame, lookback: int):
    """
    回傳：
        box_upper, box_lower, anchor_date, anchor_price (anchor low)
    """
    df = df_1d.tail(lookback).copy().reset_index(drop=True)

    # Step 1：找最大量錨點
    anchor_idx   = int(df['volume'].idxmax())
    anchor_date  = df.index[anchor_idx] if hasattr(df.index[anchor_idx], 'date') else anchor_idx
    anchor_date  = df_1d.tail(lookback).index[anchor_idx]

    # Step 2：箱體下緣 = 錨點之後（含錨點）所有 K 棒最低點
    box_lower = float(df['low'].iloc[anchor_idx:].min())

    # Step 3：箱體上緣 = 錨點之後（含錨點）所有 K 棒最高點
    box_upper = float(df['high'].iloc[anchor_idx:].max())

    # Step 4：若最新收盤跌破下緣 → 重建
    latest_close = float(df['close'].iloc[-1])
    rebuilt = False
    if latest_close < box_lower:
        breakdown_zone = df.iloc[anchor_idx:]
        below_mask = breakdown_zone['close'] < box_lower
        if below_mask.any():
            first_below = below_mask.idxmax()
            post_break  = df.iloc[first_below:].copy()
            if len(post_break) >= 3:
                new_anchor_rel = int(post_break['volume'].idxmax()) - first_below
                new_anchor_rel = max(new_anchor_rel, 0)
                box_lower  = float(post_break['low'].iloc[new_anchor_rel])
                box_upper  = float(post_break['high'].iloc[new_anchor_rel:].max())
                anchor_date = df_1d.tail(lookback).index[first_below + new_anchor_rel]
                rebuilt = True

    return box_upper, box_lower, anchor_date, rebuilt

box_upper, box_lower, anchor_date, rebuilt = detect_box(df, BOX_LOOKBACK)

print(f"\n{'='*50}")
print(f"箱體偵測結果（{SYMBOL}，日線 ×{BOX_LOOKBACK}）")
print(f"  錨點日期：{pd.Timestamp(anchor_date).date()}")
print(f"  箱體下緣：{box_lower:.2f}")
print(f"  箱體上緣：{box_upper:.2f}")
print(f"  箱體寬度：{box_upper - box_lower:.2f}  ({(box_upper/box_lower-1)*100:.2f}%)")
if rebuilt:
    print(f"  ⚠️  原箱體已失效（跌破下緣），已重新建立新箱體")
print(f"{'='*50}\n")

# ── 畫圖 ─────────────────────────────────────────────────────────────────

# 標記錨點
anchor_series = pd.Series(index=df.index, dtype=float)
if anchor_date in df.index:
    anchor_series[anchor_date] = df.loc[anchor_date, 'high'] * 1.005

# 使用最近 90 天畫圖（太多了看不清楚）
plot_df = df.tail(90).copy()

# 在畫圖區間內標記錨點
anchor_plot = pd.Series(index=plot_df.index, dtype=float)
if anchor_date in plot_df.index:
    anchor_plot[anchor_date] = plot_df.loc[anchor_date, 'high'] * 1.005

# 在畫圖區域建立箱體水平線（以 hlines 參數傳入）
hlines_vals   = [box_upper, box_lower]
hlines_colors = ['#2196F3', '#2196F3']   # 藍色箱體邊線
hlines_styles = ['--', '--']
hlines_widths = [1.5, 1.5]

# 成交量顏色（錨點用橘紅標記）
vcolors = []
for ts in plot_df.index:
    if ts == anchor_date:
        vcolors.append('#FF5722')   # 錨點：橘紅
    elif plot_df.loc[ts, 'close'] >= plot_df.loc[ts, 'open']:
        vcolors.append('#26A69A')   # 上漲：綠
    else:
        vcolors.append('#EF5350')   # 下跌：紅

# 箱體填色（用 fill_between）
fig_title = (
    f"{SYMBOL} 日線 — Volume-Anchored Box\n"
    f"上緣 {box_upper:.2f}  /  下緣 {box_lower:.2f}  /  錨點 {pd.Timestamp(anchor_date).date()}"
)

# 加入 apds（額外面板）
apds = [
    mpf.make_addplot(anchor_plot, type='scatter', markersize=80,
                     marker='^', color='#FF5722', panel=0)
]

style = mpf.make_mpf_style(
    base_mpf_style   = 'nightclouds',
    gridstyle        = '--',
    gridcolor        = '#333',
    facecolor        = '#131722',
    figcolor         = '#131722',
    rc               = {
        'axes.labelcolor': '#D1D4DC',
        'xtick.color':     '#D1D4DC',
        'ytick.color':     '#D1D4DC',
        'figure.titlesize': 11,
    }
)

fig, axes = mpf.plot(
    plot_df,
    type       = 'candle',
    style      = style,
    title      = fig_title,
    volume     = True,
    addplot    = apds,
    hlines     = dict(
        hlines      = hlines_vals,
        colors      = hlines_colors,
        linestyle   = hlines_styles,
        linewidths  = hlines_widths,
    ),
    volume_panel   = 1,
    panel_ratios   = (3, 1),
    figsize        = (16, 9),
    returnfig      = True,
    vlines         = dict(
        vlines     = [str(anchor_date)[:10]],
        linewidths = 1,
        colors     = '#FF5722',
        alpha      = 0.4,
    ) if anchor_date in plot_df.index else None,
)

ax_main = axes[0]

# 箱體半透明填色
x_start = 0
x_end   = len(plot_df) - 1
ax_main.axhspan(box_lower, box_upper, xmin=0, xmax=1,
                alpha=0.07, color='#2196F3', zorder=0)

# 圖例
patch_box    = mpatches.Patch(color='#2196F3', alpha=0.5, label=f'箱體 {box_lower:.0f} ~ {box_upper:.0f}')
patch_anchor = mpatches.Patch(color='#FF5722', label=f'錨點 ({pd.Timestamp(anchor_date).date()})')
ax_main.legend(handles=[patch_box, patch_anchor],
               loc='upper left', framealpha=0.3,
               labelcolor='white', fontsize=10)

# 儲存
out_path = f"tools/box_{SYMBOL}_{pd.Timestamp('today').strftime('%Y%m%d')}.png"
fig.savefig(out_path, dpi=150, bbox_inches='tight',
            facecolor='#131722', edgecolor='none')
print(f"圖表已儲存：{out_path}")
plt.show()
