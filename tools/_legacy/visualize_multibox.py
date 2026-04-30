"""
TITAN — 多箱體視覺化工具
顯示從指定日期到今天的完整箱體序列，包含所有歷史失效箱體與當前有效箱體。

執行：
    python tools/visualize_multibox.py [--symbol ETHUSDT] [--days 270] [--formation 5]

箱體偵測邏輯：
    1. 從 search_start 找最大成交量 K 棒作為錨點
    2. 錨點後 formation_days 根 K 棒為「成型期」（上下緣可彈性調整）
    3. 成型期結束後 box_lower 固定；box_upper 可隨假突破向上延伸
    4. 當 close < box_lower → 箱體失效，從此點往後找新錨點
    5. 重複直到沒有新的失效
"""

import argparse
import ccxt
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

# ── 參數 ────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument('--symbol',    default='ETHUSDT')
parser.add_argument('--days',      default=270, type=int)
parser.add_argument('--formation', default=5,   type=int,
                    help='錨點後幾根 K 棒為成型期（box_lower 可調整）')
parser.add_argument('--min_days',  default=7,   type=int,
                    help='最短箱體有效天數（過濾雜訊）')
args = parser.parse_args()

SYMBOL     = args.symbol
DAYS       = args.days
FORM_DAYS  = args.formation
MIN_DAYS   = args.min_days

# ── 抓資料（公開 API）────────────────────────────────────────────────────
print(f"正在抓 {SYMBOL} 日線 {DAYS} 根...")
ex = ccxt.binance({'options': {'defaultType': 'future'}})
ohlcv = ex.fetch_ohlcv(f"{SYMBOL[:3]}/USDT:USDT", timeframe='1d', limit=DAYS)

df = pd.DataFrame(ohlcv, columns=['ts','open','high','low','close','volume'])
df['ts'] = pd.to_datetime(df['ts'], unit='ms')
df = df.astype({'open':float,'high':float,'low':float,'close':float,'volume':float})
df = df.reset_index(drop=True)

print(f"資料範圍：{df['ts'].iloc[0].date()} ~ {df['ts'].iloc[-1].date()}  ({len(df)} 根)")

# ── 多箱體偵測算法 ────────────────────────────────────────────────────────
def detect_all_boxes(df, form_days=5, min_days=7, anchor_window=30):
    """
    順序掃描，回傳所有箱體清單（含已失效的歷史箱體）。

    關鍵修正：不在整段剩餘期間找全局最大量，
    而是在每個 epoch 起點的前 anchor_window 根 K 棒內找錨點，
    避免未來高量 K 棒被提前吸走。

    每個 box 是一個 dict：
        anchor_idx, anchor_date, start_idx, end_idx,
        lower, upper, invalidated, color
    """
    BOX_COLORS = [
        '#2196F3',  # 藍（第 1 個）
        '#FFC107',  # 黃（第 2 個，假突破延伸）
        '#9C27B0',  # 紫（第 3 個）
        '#4CAF50',  # 綠（第 4 個）
        '#E91E63',  # 粉紅（第 5 個）
        '#00BCD4',  # 青（第 6 個）
        '#FF5722',  # 橘（第 7 個）
    ]

    n      = len(df)
    boxes  = []
    search = 0
    ci     = 0

    while search < n - form_days - 2:
        # Step 1：在 anchor_window 內找最大量錨點（避免全局最大量搶走未來位置）
        win_end    = min(search + anchor_window, n)
        vol_sub    = df['volume'].iloc[search : win_end]
        anchor_idx = int(vol_sub.idxmax())       # global index
        anchor_date = df['ts'].iloc[anchor_idx]

        # Step 2：成型期（錨點 + form_days）
        form_end  = min(anchor_idx + form_days, n - 1)
        box_lower = float(df['low'].iloc[anchor_idx : form_end + 1].min())
        box_upper = float(df['high'].iloc[anchor_idx : form_end + 1].max())

        start_idx    = anchor_idx
        end_idx      = form_end
        invalidated  = False
        inv_idx      = None

        # Step 3：成型期後逐根掃描
        for i in range(form_end + 1, n):
            h = float(df['high'].iloc[i])
            c = float(df['close'].iloc[i])

            # 假突破 → box_upper 自動延伸
            if h > box_upper:
                box_upper = h

            end_idx = i

            # 失效條件：收盤跌破 box_lower
            if c < box_lower:
                invalidated = True
                inv_idx     = i
                break

        # 過濾過短的箱體（雜訊）
        duration = end_idx - start_idx
        if duration < min_days:
            # 跳過這個錨點，往前推進一天再重試
            search = anchor_idx + 1
            continue

        boxes.append({
            'anchor_idx':  anchor_idx,
            'anchor_date': anchor_date,
            'start_idx':   start_idx,
            'end_idx':     end_idx,
            'lower':       box_lower,
            'upper':       box_upper,
            'invalidated': invalidated,
            'color':       BOX_COLORS[ci % len(BOX_COLORS)],
        })
        ci += 1

        if invalidated and inv_idx is not None:
            search = inv_idx
        else:
            break

    return boxes


boxes = detect_all_boxes(df, form_days=FORM_DAYS, min_days=MIN_DAYS)

# ── 輸出摘要 ─────────────────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"偵測到 {len(boxes)} 個箱體：")
for i, b in enumerate(boxes):
    status     = "已失效" if b['invalidated'] else "有效（當前）"
    start_d    = df['ts'].iloc[b['start_idx']].date()
    end_d      = df['ts'].iloc[b['end_idx']].date()
    width_pct  = (b['upper'] / b['lower'] - 1) * 100
    print(f"  Box {i+1} [{status}]  {start_d} ~ {end_d}")
    print(f"    錨點：{b['anchor_date'].date()}  |  "
          f"下緣：{b['lower']:.2f}  |  上緣：{b['upper']:.2f}  |  "
          f"寬度：{width_pct:.1f}%")
print(f"{'='*65}\n")

# ── 畫圖 ─────────────────────────────────────────────────────────────────
fig, (ax, ax_v) = plt.subplots(
    2, 1, figsize=(20, 10),
    gridspec_kw={'height_ratios': [4, 1]},
    sharex=True
)
BG = '#131722'
fig.patch.set_facecolor(BG)
for a in [ax, ax_v]:
    a.set_facecolor(BG)
    for spine in a.spines.values():
        spine.set_color('#2A2E39')
    a.tick_params(colors='#D1D4DC', labelsize=8)

n = len(df)

# 蠟燭
for i, row in df.iterrows():
    o, h, l, c = row['open'], row['high'], row['low'], row['close']
    col = '#26A69A' if c >= o else '#EF5350'
    ax.plot([i, i], [l, h], color=col, linewidth=0.8, zorder=2)
    body_h = max(abs(c - o), h * 0.001)
    rect = mpatches.FancyBboxPatch(
        (i - 0.35, min(o, c)), 0.7, body_h,
        boxstyle='square,pad=0',
        facecolor=col, edgecolor='none', zorder=2
    )
    ax.add_patch(rect)

# 成交量
for i, row in df.iterrows():
    col = '#26A69A' if row['close'] >= row['open'] else '#EF5350'
    ax_v.bar(i, row['volume'], color=col, alpha=0.6, width=0.8)

# 箱體矩形
legend_handles = []
for b in boxes:
    s, e     = b['start_idx'], b['end_idx']
    lo, hi   = b['lower'],     b['upper']
    col      = b['color']
    invalid  = b['invalidated']
    alpha_bg = 0.06 if invalid else 0.13
    lw       = 1.2
    ls       = '--' if invalid else '-'
    la       = 0.55 if invalid else 0.9

    # 填色
    ax.fill_betweenx([lo, hi], s, e, alpha=alpha_bg, color=col, zorder=0)

    # 上下緣
    ax.plot([s, e], [hi, hi], color=col, lw=lw, ls=ls, alpha=la, zorder=3)
    ax.plot([s, e], [lo, lo], color=col, lw=lw, ls=ls, alpha=la, zorder=3)

    # 側邊線（起點）
    ax.plot([s, s], [lo, hi], color=col, lw=0.7, ls=':', alpha=la * 0.6, zorder=3)

    # 錨點標記（三角形）
    ai  = b['anchor_idx']
    tip = df['low'].iloc[ai] * 0.997
    ax.scatter(ai, tip, marker='v', color=col, s=50, zorder=5)

    # 錨點垂直線
    ax_v.axvline(x=ai, color=col, alpha=0.35, lw=1)

    # 在箱體右側標註數字
    label_x = e + 0.5
    mid_y   = (lo + hi) / 2
    ax.annotate(
        f"{hi:.0f}\n{lo:.0f}",
        xy=(label_x, mid_y),
        fontsize=7, color=col, alpha=la,
        va='center', ha='left'
    )

    # 圖例
    tag    = f"Box {boxes.index(b)+1}"
    status = " (失效)" if invalid else " (active)"
    legend_handles.append(
        mpatches.Patch(color=col, alpha=0.8, label=f"{tag}{status}  {lo:.0f}~{hi:.0f}")
    )

# x 軸刻度（日期）
step = max(1, n // 14)
ticks = list(range(0, n, step))
ax_v.set_xticks(ticks)
ax_v.set_xticklabels(
    [df['ts'].iloc[i].strftime('%y/%m/%d') for i in ticks],
    rotation=35, ha='right', fontsize=7.5, color='#D1D4DC'
)

ax.set_xlim(-1, n + 6)
ax.set_ylabel('Price (USDT)', color='#D1D4DC', fontsize=9)
ax_v.set_ylabel('Volume', color='#D1D4DC', fontsize=9)
ax.set_title(
    f'{SYMBOL} Daily — Volume-Anchored Multi-Box  '
    f'({df["ts"].iloc[0].date()} ~ {df["ts"].iloc[-1].date()})',
    color='#D1D4DC', fontsize=11, pad=10
)
ax.legend(
    handles=legend_handles, loc='upper right',
    framealpha=0.25, labelcolor='white', fontsize=8.5,
    frameon=True, edgecolor='#444'
)
ax.grid(axis='y', color='#2A2E39', linewidth=0.5, linestyle='--', alpha=0.5)

plt.tight_layout(h_pad=0.5)
out = f"tools/multibox_{SYMBOL}_{pd.Timestamp('today').strftime('%Y%m%d')}.png"
fig.savefig(out, dpi=150, bbox_inches='tight', facecolor=BG)
print(f"圖表已儲存：{out}")
plt.show()
