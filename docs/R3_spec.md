# R3 Crypto Futures Strategy — 正式規格 (v1.0)

| 欄位 | 內容 |
|---|---|
| 版本 | v1.0 (initial spec lock) |
| 鎖檔日期 | 2026-04-30 |
| 決策者 | RAYZ (BOSS) |
| 規格撰寫 | MIA |
| 狀態 | LOCKED — 進入工程實作階段 |

---

## R3 命名意義

```
R3 = Regime Filter + Risk Engine + Return Targeting
```

- **Regime Filter**：先判斷市場狀態（趨勢 / 盤整 / 極端擁擠 / 高風險），不在所有市場下都交易
- **Risk Engine**：每筆固定風險（百分比），槓桿是「停損距離 + 風險」反推的結果，不是設計變數
- **Return Targeting**：用交易頻率 × R 值追求日均 50–150 USDT，不靠高槓桿賭單筆爆擊

---

## 0. 整體架構

```
                  ┌─────────────────────────────┐
                  │   Regime Classifier (4H)    │
                  └──────────────┬──────────────┘
                                 │
              ┌──────┬───────────┼───────────┬──────┐
              │      │           │           │      │
            [A]    [B]         [C]         [D1]   [D2]
          趨勢盤  盤整盤    極端擁擠盤   市場風險  系統風險
              │      │           │           │      │
              ▼      ▼           ▼           ▼      ▼
         趨勢回踩  均值回歸  Funding反轉  Emergency  系統保命
                                          Tight Stop  退出
                                 │
                                 ▼
                  ┌─────────────────────────────┐
                  │  Maker first → Taker fallback│
                  │  5M 執行層 + Limit timeout   │
                  └─────────────────────────────┘
                                 │
                                 ▼
                  ┌─────────────────────────────┐
                  │  Risk Engine (每筆 0.75-1%) │
                  │  Portfolio total ≤ 1.5%     │
                  └─────────────────────────────┘
```

---

## 1. 交易標的

| 階段 | 標的 |
|---|---|
| 首發 | `BTC/USDT:USDT`、`ETH/USDT:USDT` 永續合約 |
| 第二階段（穩定後） | 加 `SOL/USDT:USDT` |

**首發禁止**：山寨幣、流動性差的標的、funding interval 非 8h 的標的。

---

## 2. 時間週期

| 用途 | 週期 |
|---|---|
| 大方向判斷 | 4H |
| 主訊號 | 1H |
| 進出場執行 | 5M |
| D2 系統風控 | realtime / 1M |

---

## 3. Regime Filter

### 3.1 Regime A — 趨勢盤

進入條件（全部成立）：
- `ADX(4H) > 22`
- `EMA50(4H) > EMA200(4H)`（只做多）或 `EMA50(4H) < EMA200(4H)`（只做空）
- `extreme_vol == False`（見 §3.5）
- `|funding_z_score| < 2.5`

**啟用策略**：趨勢回踩續行（§4）

### 3.2 Regime B — 盤整盤

進入條件（全部成立）：
- `ADX(4H) < 18`
- 1H Close 在 `4H EMA200 ± 0.5 × ATR_4H` 範圍內
- `BB_width(1H, 20)` 處於近 90 天 10–50 分位
- `|funding_z_score| < 1.0`

**啟用策略**：均值回歸（§5）

### 3.3 Regime C — 極端擁擠盤

進入條件（任一）：
- `funding_z_score > 2.5` 且 `premium_z_score > 2.0`
- `funding_z_score < -2.5` 且 `premium_z_score < -2.0`

**啟用策略**：Funding / Premium 反轉（§6，副策略）

### 3.4 Regime D — 禁止新倉

#### D1 — 市場風險

任一觸發即進入 D1：
- `extreme_vol == True`
- 連續 2 根 1H K 線振幅超過 `ATR_1H × 2.5`
- 重大消息事件視窗（手動觸發）
- `|funding_z_score| > 3.0`
- 當日已虧損 ≤ −2%
- 連續虧損 ≥ 4 筆

#### D2 — 系統風險

任一觸發即進入 D2：
- WebSocket 中斷
- API 延遲異常（連續 3 次 > 2 秒）
- K 線資料缺失
- 保證金資料讀取失敗
- 訂單回報異常

### 3.5 Extreme Vol 定義（Q5 + Q13）

```
ATR_pct = ATR(1H, 14) / close
```

**Warmup 規則**：

| 期間 | 是否可交易 | extreme_vol 判定 |
|---|---|---|
| Day 1 ~ 30 | ❌ 不可交易（僅累積資料） | — |
| Day 31 ~ 90 | ✅ 可交易 | `ATR_pct > 0.04` (4%) |
| Day 91+ | ✅ 可交易 | `ATR_pct > percentile(ATR_pct, 90D rolling, 95)` |

### 3.6 Regime 更新頻率（Q15）

| 切換 | Trigger | 時間框 |
|---|---|---|
| A ↔ B 切換 | `on_4h_candle_close` | 4H 收盤 |
| 進入 D1 | `on_1h_candle_close_or_faster` | 1H 收盤或更快 |
| 進入 D2 | `realtime` | 即時事件 |

### 3.7 Regime 切換時的持倉處理（Q1, Q2, Q16）

#### A → B 切換
1. **不**強制市價平倉
2. **不**加倉
3. **不**開同策略新倉
4. 原倉位進入 **Tight Trailing Mode**（見 §7.2）
5. **一旦進入 Tight Trailing 就不恢復**（即使 Regime 又回 A，也不放寬）

#### 進入 D1（市場風險）
1. 不平倉
2. 取消所有未成交掛單
3. 既有倉位改用 **Emergency Tight Stop**（見 §7.3）
4. 不開新倉

#### 進入 D2（系統風險）依資料可信度分級

| 觸發條件 | 處理 |
|---|---|
| WebSocket 中斷 | reduce-only 100% 退出 |
| API 延遲異常 | reduce-only 100% 退出 |
| 保證金資料讀取失敗 | reduce-only 100% 退出 |
| 訂單回報異常 | reduce-only 100% 退出 |
| K 線缺失 1–2 根 | reduce-only 50% 退出 |
| K 線缺失 > 2 根 | reduce-only 100% 退出 |
| 無法確認倉位大小 | 不送反向開倉單；查倉成功後才允許 reduce-only |

優先使用交易所 `close-position` reduce-only 指令。

---

## 4. 主策略 1 — 趨勢回踩續行

### 4.1 進場條件

#### 做多（全部成立）
1. Regime == A 且 `EMA50(4H) > EMA200(4H)` 且 `ADX(4H) > 22`
2. `1H low ≤ EMA20 + 0.3 × ATR_1H` 且 `1H close ∈ [EMA20 − 0.3×ATR_1H, EMA20 + 0.3×ATR_1H]`（EMA50 同理）— **Q21**
3. **過去 5 根 1H 內 `min(RSI_1H) ≤ 50`** 且 `RSI_1H[i] > RSI_1H[i-1]` 且 `RSI_1H[i] > 50` — **Q22**
4. 5M 出現轉強確認（§4.2）
5. `funding_z_score < 2.0`

#### 做空（對稱）
1. Regime == A 且 `EMA50(4H) < EMA200(4H)` 且 `ADX(4H) > 22`
2. `1H high ≥ EMA20 − 0.3 × ATR_1H` 且 close 在 EMA ± 0.3×ATR_1H
3. 過去 5 根 1H 內 `max(RSI_1H) ≥ 50` 且 `RSI_1H[i] < RSI_1H[i-1]` 且 `RSI_1H[i] < 50`
4. 5M 出現轉弱確認（§4.2）
5. `funding_z_score > -2.0`

### 4.2 5M 確認（Q11）— 三條件二選二

#### 做多轉強
1. `5M close > EMA9` 且 `EMA9 slope > 0`（即 `EMA9[i] > EMA9[i-2]`）— **Q20**
2. `5M close > max(high[i-3..i-1])`（突破前 3 根 high）
3. **Bullish engulfing** 或 **strong close candle**

#### 做空轉弱
1. `5M close < EMA9` 且 `EMA9[i] < EMA9[i-2]`
2. `5M close < min(low[i-3..i-1])`
3. **Bearish engulfing** 或 **weak close candle**

#### 形態定義

```
strong close:    close > open
                 (close - low) / (high - low) ≥ 0.7
                 body / (high - low) ≥ 0.5

weak close:      close < open
                 (high - close) / (high - low) ≥ 0.7
                 body / (high - low) ≥ 0.5

bullish engulfing:  current.close > previous.open
                    current.open  < previous.close
                    current.body  > previous.body × 1.1

bearish engulfing:  對稱版
```

### 4.3 訊號有效窗口（Q23）

1H 訊號產生後，**只在下一根 1H 期間（即 1H 收盤後 12 根 5M 內）**有效；第 13 根 5M 起訊號失效，必須重新等下一根 1H 收盤。

### 4.4 進場方式（Q12, Q26）

**第一選擇：Limit Maker**

```
做多 limit_price = min(
    current_bid - 1 tick,
    EMA20_1H,
    signal_5M_close - 0.1 × ATR_5M
)

做空 limit_price = max(
    current_ask + 1 tick,
    EMA20_1H,
    signal_5M_close + 0.1 × ATR_5M
)
```

**Position Quantity 計算（Q26）**：

```
risk_amount  = equity × risk_per_trade × risk_multiplier
sl_distance  = |limit_price - stop_price|
quantity     = risk_amount / sl_distance
```

**Timeout（Q12, Q29）**：
- 限價單存活 = 2 根 5M K（10 分鐘）
- timeout 後若訊號仍有效（重新跑一次完整訊號 check）→ 可重新掛一次
- 若已偏離目標太遠 → 放棄
- **不允許 chase**

**Taker fallback 條件（全部成立才允許）**：
- 訊號仍有效
- spread 正常（< 平均 spread × 1.5）
- `estimated_slippage_bps < 2`（用 spread / 2 估）
- 趨勢強度高（`ADX_4H > 28`）

### 4.5 部分成交（Q27）

| 情境 | 處理 |
|---|---|
| 已成交部分 | 視為正式進場，立即掛 reduce-only SL |
| Timeout 未成交剩餘 | 取消 |
| Risk / R-multiple 分母 | 用**實際成交數量** |

### 4.6 停損（Q9 + 結構低點）

```
SL_long  = max(
    最近 confirmed pivot low (1H, N=5)  - 0.2 × ATR_1H,
    entry - 1.8 × ATR_1H   # fallback 若無有效 pivot
) 取較近 entry 的那個（更保守）

SL_short = 對稱
```

`confirmation_delay_bars = 5` — 不能偷看未來的 pivot。

### 4.7 停利

```
TP1 = 1R   → 出 50%
TP2 = 2.5R ~ 3R → 剩餘部位 + trailing
```

### 4.8 移動停利（標準模式）

| 條件 | 動作 |
|---|---|
| 浮盈 ≥ 1R | SL 移到 breakeven |
| 浮盈 ≥ 2R | trailing：取 `1H EMA20` 與 `entry + (peak − 1.5×ATR_1H)` 的較高者 |

### 4.9 部分平倉後的 Risk

TP1 出 50%、SL 移到 breakeven 後，剩餘 50% 視為 **risk = 0**（不計入 portfolio total open risk）。

### 4.10 同策略連續訊號 Cooldown（Q29）

| 出場原因 | Cooldown |
|---|---|
| SL 出場 | 同幣同策略 cooldown 1 根 1H |
| TP 出場 | 不 cooldown |

---

## 5. 主策略 2 — 均值回歸

### 5.1 進場條件

#### 做多（全部成立）
1. Regime == B
2. `1H close < BB_lower(20, 2.0)`
3. `1H close < VWAP_today − 1.5 × stdev_24h`
4. `RSI_1H(14) < 28`
5. `|funding_z_score| < 1.5`（Funding 中性）
6. 5M 止跌確認（§5.2）

#### 做空（對稱）
1. Regime == B
2. `1H close > BB_upper(20, 2.0)`
3. `1H close > VWAP_today + 1.5 × stdev_24h`
4. `RSI_1H(14) > 72`
5. `|funding_z_score| < 1.5`
6. 5M 止漲確認（§5.2）

### 5.2 5M 止跌 / 止漲確認（Q25）— 三條件二選二

**獨立於趨勢確認**，不共用「突破前 3 根 high」這種趨勢條件。

#### 多單止跌
1. `5M close > open` 且 `(close - low) / (high - low) ≥ 0.6`
2. **5M Bullish engulfing** 或 **Hammer**
3. `5M RSI(14) < 30` 且 `RSI[i] > RSI[i-1]`

#### 空單止漲
1. `5M close < open` 且 `(high - close) / (high - low) ≥ 0.6`
2. **5M Bearish engulfing** 或 **Shooting star**
3. `5M RSI(14) > 70` 且 `RSI[i] < RSI[i-1]`

### 5.3 出場

| 出場類型 | 條件 |
|---|---|
| TP（中軸） | 價格回到 `BB_middle(20)` 或 `VWAP_today` 任一觸及 |
| SL | `0.8 ~ 1.2 × ATR_1H`（取靠近結構支撐/壓力的 pivot 邊界） |
| Time stop | 持倉 ≥ 12 小時 |

---

## 6. 副策略 — Funding / Premium 反轉

### 6.1 進場條件

#### 做空（市場過熱）
1. Regime == C
2. `funding_z_score > 2.5`
3. `premium_z_score > 2.0`
4. `(mark_price - index_price) / index_price > 0.0015`（≥ 15 bps）
5. `RSI_1H > 75`
6. **過去 5 根 1H 無新高**（價格不再創高）— Tier 3 default

#### 做多（市場過冷）
- 對稱規則

### 6.2 與既有反向倉位衝突（Q18）

| 既有倉位 | Funding 反轉訊號 | 動作 |
|---|---|---|
| 多單持倉 | 空 reversal | **等待**（不啟動，不減倉） |
| 空單持倉 | 多 reversal | **等待** |
| 同方向 | 同方向 reversal | 視作加倉訊號，遵守 portfolio risk cap |

### 6.3 出場

| 出場類型 | 條件 |
|---|---|
| TP | 價格回到 `mark_price` 或 `VWAP` 附近 |
| SL | `0.8 ~ 1.5 × ATR_1H` |
| Time stop | 持倉 ≥ 12 小時 |

---

## 7. 移動停利模式

### 7.1 標準模式

定義於 §4.8。

### 7.2 Tight Trailing Mode（Q1, Q14, Q16）

啟動條件：A → B 切換

```
Pivot timeframe: 5M
Pivot N        : 3
Confirm delay  : 3 bars

多單 stop = max(
    current_stop,
    latest_confirmed_5M_pivot_low - 0.1 × ATR_5M
)

空單 stop = min(
    current_stop,
    latest_confirmed_5M_pivot_high + 0.1 × ATR_5M
)
```

附加規則：
- 只能**收緊**，不能放寬
- 浮盈 < 0：維持原 SL，不放寬
- 浮盈 ≥ 1R：至少 breakeven
- 浮盈 ≥ 1.5R：鎖 0.5R
- **一旦啟動就鎖死直到該倉位平倉**（即使 Regime 回 A 也不恢復標準模式）

### 7.3 Emergency Tight Stop（Q2 / D1）

啟動條件：進入 Regime D1

```
浮盈 ≥ 0.5R：至少 breakeven
浮盈 ≥ 1R  ：鎖 0.3R ~ 0.5R
浮虧      ：原 SL 不放寬
            若 5M 反向破結構（confirmed pivot），提前出場
```

### 7.4 Trailing Trigger 機制（Tier 3）

實盤 trailing stop 使用 `STOP_MARKET` reduce-only，由 **intrabar high/low hit** 觸發，不等收盤確認。

---

## 8. 風險管理

### 8.1 三檔風險規格

| 階段 | 單筆風險 | 同時最大風險 | 每日最大虧損 | 每週最大虧損 | 目標日均 |
|---|---|---|---|---|---|
| 首發版本 | 0.75% | 1.5% | -2.0% | -6.0% | 50–90 USDT |
| 成熟版本 | 1.0% | 2.0% | -2.5% | — | 70–120 USDT |
| 激進版本 | 1.25% | 2.5% | -3.0% | — | 100–150 USDT |

**首發強制使用「首發版本」**。

### 8.2 倉位計算（Q26）

```
risk_amount  = equity × risk_per_trade × risk_multiplier
sl_distance  = |limit_price - stop_price|   # 用 limit_price 算
quantity     = risk_amount / sl_distance
notional     = quantity × limit_price
leverage     = notional / equity            # 結果，非設計變數
```

### 8.3 Equity 基準（Q28）

```
equity = wallet_balance + realized_pnl - max(0, -unrealized_pnl)
       = wallet_balance + realized_pnl - unrealized_pnl_if_negative
```

正浮盈不放大風險；負浮虧即時縮小可用風險。

### 8.4 連虧計數（Q6, Q7）

```
consecutive_losses ≥ 2：
    risk_multiplier = 0.5

恢復條件：
    下一筆 realized_R_multiple ≥ 0.3 → risk_multiplier = 1.0
    （只是小賺 < 0.3R 不恢復）

跨日清零：
    daily_loss = 0
    daily_trade_count = 0
    daily_realized_pnl = 0

跨日不清零：
    consecutive_losses
    risk_multiplier
```

連虧 4 觸發當日停止後，跨日恢復交易但 `risk_multiplier = 0.5`，直到一筆 ≥ 0.3R 獲利才恢復（Tier 3 default）。

### 8.5 R-multiple 計算

```
R = risk_amount    # 該筆下單時鎖定的風險金額
R_multiple = realized_pnl_net / R   # 含 fees + funding
```

### 8.6 BTC + ETH Correlation Haircut（Q8）

```
combined_risk_btc_eth_same_direction ≤ base_risk_per_trade  # 即 ≤ 1%
```

依時間順序分配：

| 先觸發 | 配置 |
|---|---|
| BTC 先 | BTC 0.7%，ETH 後到最多 0.3% |
| ETH 先 | ETH 0.5%，BTC 後到最多 0.5% |
| 同根 1H 同時 | BTC 0.6%，ETH 0.4% |

### 8.7 Portfolio Risk Cap

```
total_open_risk ≤ 1.5%   # 首發

per-strategy cap:
  trend             ≤ 1.5%
  mean_reversion    ≤ 1.0%
  funding_reversal  ≤ 0.75%
```

優先級：既有倉位風控 > Trend > Mean Reversion > Funding Reversal

### 8.8 策略間反向倉位（Q24）

**同幣已有倉位時，禁止開反向新倉**。
- 不啟用 hedge mode
- 不強制平掉舊倉
- 等舊倉自然 TP / SL / trailing exit 後，新方向訊號才允許進場

### 8.9 硬規則（直接寫進程式碼，不允許人工干預）

1. 當日虧損 ≤ -2% → 停止開新倉
2. 連續 2 筆虧損 → 下一筆 risk × 0.5
3. 連續 4 筆虧損 → 當日停止
4. 同方向（BTC + ETH）同時持倉時做 correlation haircut
5. Funding 極端時（|z| > 2.5）不追同方向（Regime D1）
6. Extreme vol（§3.5）不開新倉
7. 任一 D2 條件 → 停止策略，啟動保命退出
8. 未通過 L0–L6 全部驗證的策略，不可 live
9. 實盤前至少 dry-run 30 天
10. **驗證 Fail 時不允許偷偷調參讓它通過**

---

## 9. Funding Z-score（Q3, Q4）

### 9.1 計算

```
lookback_days       = 90
funding_interval    = 8h（首發只跑幣安永續，預設 8h）
expected_samples    = 90d × 24h / 8h = 270
min_samples_required = 120

funding_z = (current_rate - mean(rates, 90d)) / stdev(rates, 90d)
```

若 `samples < 120`：
- 不啟用 funding reversal
- funding filter 只做輕量權重（不做強訊號）

### 9.2 用途分流

| 用途 | 資料 |
|---|---|
| Signal（判斷擁擠度） | `funding_rate` 的 z-score |
| PnL（實際損益） | `funding_amount = position_notional × funding_rate × direction_adj` |

`direction_adj` 規則：
- `funding_rate > 0`：long 付給 short → long.capital -= notional × rate；short.capital += notional × rate
- `funding_rate < 0`：反之

Funding 結算 PnL **計入 daily loss limit**（Tier 3 default）。

### 9.3 結算時點

UTC 00:00 / 08:00 / 16:00 對齊（不是 since 起點 +N×8h）。

---

## 10. 驗證體系 L0 ~ L6

### 10.1 流程

```
L0 純回測 → L1 Walk-Forward → L2 MCPT → L3 Block Bootstrap
   → L4 Bonferroni → L5 最終 OOS → L6 Regime 分層
        ↓
    通過 → Shadow / Dry-run
    Fail → 停止；不允許偷偷調參（Q5 工程紀律）
```

### 10.2 樣本切分

| 區段 | 期間 |
|---|---|
| 研究樣本 (IS) | 2021-01-01 ~ 2023-12-31 |
| 滾動驗證 | 2024-01-01 ~ 2025-03-31 |
| 最終 OOS | 2025-04-01 ~ 2026-03-31 |

### 10.3 各層門檻

#### L0 純回測
| 指標 | 門檻 |
|---|---|
| 淨 Sharpe | ≥ 1.2 |
| Calmar | ≥ 1.0 |
| MDD | ≤ 20% |
| Profit Factor | ≥ 1.15 |
| 交易數 | ≥ 250 |
| Avg trade | ≥ 1.5 × round-trip cost |

#### L1 Walk-Forward
```
train: 360 天
test : 90 天
step : 30 天
```
| 指標 | 門檻 |
|---|---|
| 聚合 OOS Sharpe | ≥ 0.8 |
| OOS / IS CAGR ratio | ≥ 0.6 |
| 虧損 fold | 不可過多 |
| 參數穩定性 | 不可劇烈跳動 |

#### L2 MCPT（5000+ 路徑）
雙層擾動：
- **價格擾動**：ARMA-GARCH 殘差重抽
- **交易擾動**：每筆 0–1 bar latency、0–3 bps 額外滑價、5% 漏單率、fee shock

| 指標 | 門檻 |
|---|---|
| 5% 分位 CAGR | > 0 |
| 30 天虧損機率 | < 35% |
| Risk of ruin | ≈ 0 |

#### L3 Block Bootstrap
- 方法：**Stationary Bootstrap**（Politis-Romano）
- Block length：1H 策略用 24–72 bars 起測

| 指標 | 門檻 |
|---|---|
| Bootstrap expectancy 95% CI 下界 | > 0 |
| 95% 分位 MDD | < 風險上限 |

#### L4 Bonferroni
```
m = 整個研究過程中曾經被測試的策略變體 / 參數組總數
α* = 0.05 / m
```

| 指標 | 門檻 |
|---|---|
| Adjusted p-value | < 0.05 |
| **PBO via CSCV** | < 0.20 |
| **DSR** | > 0.95 |

#### L5 最終 OOS（2025-04-01 ~ 2026-03-31）
此區段**不可調參**。

| 指標 | 門檻 |
|---|---|
| 淨 Sharpe | ≥ 0.7 |
| Calmar | ≥ 0.8 |
| Profit Factor | ≥ 1.08 |
| MDD | 不爆 |

#### L6 Regime 分層
分層維度：trend 強弱 / vol 高低 / funding 極端正中性負 / bull-bear

| 條件 | 門檻 |
|---|---|
| 主要 regime expectancy | > 0 |
| 收益不可只來自單一 regime | 至少 2 個 regime 為正 |
| 虧錢 regime | 必須能被 filter 關掉 |

---

## 11. 部署流程

```
研究驗證 (L0-L6 全通過)
    ↓
Shadow（live feed，零下單）
    ↓
Dry-run（模擬單，30 天）
    ↓
Canary（極小資金 live，30-60 天）
    ↓
Scale-up（逐步提高組合密度）
```

每個階段不通過退出標準時，**禁止跨階段**。

---

## 12. Q1–Q29 決策歷史對照表

| Q | 主題 | 決議 | 章節 |
|---|---|---|---|
| Q1 | A→B 切換處理 | Tight Trailing Mode | §3.7, §7.2 |
| Q2 | D 觸發處理 | D1 Emergency Tight；D2 reduce-only 分級 | §3.7, §7.3 |
| Q3 | Funding lookback | 90 天，min 120 samples | §9.1 |
| Q4 | Signal vs PnL | rate / amount | §9.2 |
| Q5 | Extreme vol | ATR_pct 90D 95-percentile | §3.5 |
| Q6 | 連虧減半 | 連虧 2 風險 ×0.5；恢復需 R≥0.3 | §8.4 |
| Q7 | 跨日清零 | daily 清零；consecutive 不清 | §8.4 |
| Q8 | BTC+ETH haircut | 合併 ≤ 1% | §8.6 |
| Q9 | Pivot | N=5, confirm 5 bars, buffer 0.2 ATR | §4.6 |
| Q10 | 資金切片 | shared equity + per-strategy cap | §8.7 |
| Q11 | 5M 確認 | 三條件二選二（趨勢） | §4.2 |
| Q12 | Maker 掛單 | pullback price + maker constraint | §4.4 |
| Q13 | Vol warmup | 0-30 不交易；31-90 ATR>4%；91+ percentile | §3.5 |
| Q14 | Tight trailing | 5M, N=3, confirm 3 | §7.2 |
| Q15 | Regime 更新頻率 | A/B 4H 收盤；D1 1H+；D2 即時 | §3.6 |
| Q16 | Trailing 不恢復 | 一旦啟動鎖死 | §7.2 |
| Q17 | D2 平倉比例 | 系統異常 100%；K 線缺 1-2 根 50% | §3.7 |
| Q18 | Funding 反轉與反向倉位 | 等待 | §6.2 |
| Q19 | BB / VWAP spec | BB(20,2.0,1H), VWAP daily UTC, dev 24h × 1.5 | §5.1 |
| Q20 | EMA9 slope | EMA9[i] > EMA9[i-2] | §4.2 |
| Q21 | 「附近」定義 | low ≤ EMA + 0.3 ATR & close 在 ±0.3 ATR | §4.1 |
| Q22 | RSI 上彎 | 過去 5 根 min ≤ 50 + 上彎 + > 50 | §4.1 |
| Q23 | 5M 確認窗口 | 1H 收盤後 12 根 5M 內 | §4.3 |
| Q24 | 反向倉位 | 同幣禁止反向新倉，等舊倉結束 | §8.8 |
| Q25 | MR 5M 確認 | 獨立止跌/止漲三條件二選二 | §5.2 |
| Q26 | Quantity 計算 | 用 limit_price | §4.4, §8.2 |
| Q27 | 部分成交 | 已成交視作進場；未成交取消 | §4.5 |
| Q28 | Equity 基準 | 保守（負浮虧扣） | §8.3 |
| Q29 | 同策略 cooldown | SL 後 1 根 1H；TP 後不 cooldown | §4.10 |
| T3.1 | 連虧 4 跨日 | 恢復交易 risk×0.5 | §8.4 |
| T3.2 | Funding 結算入帳 | 計入 daily loss | §9.2 |
| T3.3 | 不再創高 | 過去 5 根 1H 無新高 | §6.1 |
| T3.4 | Trailing trigger | intrabar hit + STOP_MARKET reduce-only | §7.4 |

---

## 13. 工程紀律（Q5 from BOSS 工程指令）

1. 所有規則寫入 strategy spec（**本文件**）
2. 所有參數集中到 `config/r3_strategy.yaml`，不 hardcode 在策略邏輯裡
3. 寫單元測試覆蓋 Q21–Q29
4. 完成後跑 `tools/validate_all.py` 的 L0–L6 驗證
5. **驗證 Fail 時，只回報失敗原因，不偷偷調參讓它通過**

---

## 14. 待 BOSS 之後決策（不影響首發開工）

下列項目 MIA 已用 Tier 3 default 開工，但未來進入 live 前需要 review：

- [ ] L4 Bonferroni 的 `m` 究竟由誰、何時、如何結算（避免事後膨脹）
- [ ] DSR / PBO via CSCV 的具體實作演算法版本
- [ ] Canary 階段的「極小資金」具體金額（建議 500–1000 USDT）
- [ ] Scale-up 各階段的 trigger 條件
- [ ] Funding Reversal「不再創高」之外，是否需要 bearish divergence 強化

---

**規格鎖檔。任何後續變更須開新版本（v1.1, v2.0…）並標註變更原因。**
