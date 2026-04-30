<p align="center">
  <img src="https://img.shields.io/badge/TITAN-R3 v1.0-blue?style=for-the-badge&logo=bitcoin&logoColor=white" alt="TITAN R3 v1.0"/>
  <img src="https://img.shields.io/badge/Python-3.14-green?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.14"/>
  <img src="https://img.shields.io/badge/Binance-Futures-yellow?style=for-the-badge&logo=binance&logoColor=white" alt="Binance Futures"/>
  <img src="https://img.shields.io/badge/Phase-5%20Spec%20Lock-brightgreen?style=for-the-badge" alt="Phase 5 Spec Lock"/>
  <img src="https://img.shields.io/badge/License-MIT-red?style=for-the-badge" alt="MIT License"/>
</p>

<h1 align="center">TITAN R3</h1>
<h3 align="center">Trading Intelligent Tactical Automation Network</h3>
<p align="center"><b>Regime Filter · Risk Engine · Return Targeting</b></p>

<p align="center">
  全自動幣安 USDT-M 永續合約交易系統<br/>
  以 Regime 路由 + 多策略互補 + 7 層驗證為核心，追求穩健日均收益
</p>

---

## 目前狀態

| 階段 | 狀態 |
|---|---|
| Phase 1 基礎建設 | ✅ 完成 |
| Phase 2 策略 + 回測引擎 | ✅ 完成 |
| Phase 3 Demo 實盤模擬（V1.x） | ✅ 完成 (Sunset) |
| Phase 4 策略迭代（V1.1 → V2 / 朋友箱體） | ✅ 完成 (Sunset) |
| **Phase 5 R3 Spec Lock** | ✅ **完成 (2026-04-30)** |
| Sprint 1 — 基礎設施升級 | 🔄 待啟動 |
| Sprint 2-5 — R3 完整實作 | ⏳ 待定 |
| Phase 6 Dry-run 30 天 | ⏳ 待定 |
| Phase 7 Canary live | ⏳ 待定 |

**當前策略版本**：R3 v1.0 — 規格已鎖檔，進入工程實作階段

**規格文件**：[`docs/R3_spec.md`](docs/R3_spec.md)

**參數設定**：[`config/r3_strategy.yaml`](config/r3_strategy.yaml)

---

## R3 是什麼

```
R3 = Regime Filter + Risk Engine + Return Targeting
```

不再追求「找到一個神策略」，而是設計一個**會自己判斷市場狀態、會控制風險、會追蹤目標收益**的系統。

| 軸線 | 設計理念 |
|---|---|
| **Regime Filter** | 先判斷現在是趨勢、盤整、極端擁擠還是高風險，**不在所有市場下都交易** |
| **Risk Engine** | 每筆固定百分比風險，**槓桿是「停損距離 + 風險」反推的結果**，不是設計變數 |
| **Return Targeting** | 用交易頻率 × R 值追求日均 50–150 USDT，**不靠高槓桿賭單筆爆擊** |

---

## 策略架構

### 四象限 Regime 分類

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
```

### 三策略互補

| 策略 | 啟動條件 | 風險上限 |
|---|---|---|
| **趨勢回踩續行** | Regime A：4H ADX>22 + EMA50/200 排列 | 1.5% |
| **均值回歸** | Regime B：4H ADX<18 + 價格貼 EMA200 | 1.0% |
| **Funding 反轉** | Regime C：funding_z > 2.5 + premium 過熱 | 0.75% |

組合 portfolio 上限：**1.5%（首發保守版）**

### 7 層驗證 L0–L6

```
L0 純回測 → L1 Walk-Forward → L2 MCPT (5000 路徑)
   → L3 Block Bootstrap → L4 Bonferroni + DSR + PBO/CSCV
   → L5 最終 OOS（不可調參） → L6 Regime 分層
```

通過 7 層才能進 Shadow → Dry-run → Canary → Scale-up。

---

## 關鍵參數（首發版本 phase1_conservative）

| 項目 | 值 |
|---|---|
| 標的 | `BTC/USDT:USDT`、`ETH/USDT:USDT` 永續 |
| 主訊號 | 1H |
| 大方向 | 4H |
| 進出場 | 5M |
| 單筆風險 | 0.75% × equity |
| 同時最大風險 | 1.5% |
| 每日虧損上限 | -2% |
| 連續虧損 | 連虧 2 → 風險 ×0.5；連虧 4 → 當日停止 |
| BTC+ETH Haircut | 同方向合併 ≤ 1% |
| 進場執行 | Limit Maker → Taker fallback（4 條件全成立才允許） |
| 限價 timeout | 2 根 5M K（10 分鐘）→ cancel；不允許 chase |
| 停損 | confirmed pivot (1H, N=5) − 0.2 ATR；fallback 1.8 ATR |
| 停利 | TP1 = 1R 出 50%；TP2 = 2.5–3R + trailing |

詳見 [`docs/R3_spec.md`](docs/R3_spec.md) §4–§9。

---

## 工程紀律

R3 專案有 **5 條鐵律**，違反任一條視為破壞規範：

1. ✅ 所有規則寫進 `docs/R3_spec.md`
2. ✅ 所有 magic number 集中於 `config/r3_strategy.yaml`，不允許 hardcode
3. ✅ 單元測試覆蓋 Q21–Q29（`tests/test_r3.py`）
4. ✅ 上線前必須通過 L0–L6 全部驗證
5. ❌ **驗證 Fail 時禁止偷偷調參讓它通過**

---

## R3 開發路線圖

| Sprint | 工作內容 | 預估工時 |
|---|---|---|
| 1 | 5m 資料載入、funding/premium API、指標庫（BB/VWAP/Funding Z/Pivot） | 5 天 |
| 2 | Regime Classifier（A/B/C/D 切換邏輯） | 5 天 |
| 3 | 趨勢回踩主策略 + Limit Maker 訂單管理 + L0–L6 全跑 | 7 天 |
| 4 | 均值回歸主策略 + Strategy Router | 5 天 |
| 5 | Funding Reversal 副策略 | 3 天 |

→ Dry-run 30 天 → Canary 小資金 → Scale-up

---

## 專案結構

```
TITAN/
├── main.py                       # live trading 主程式
├── state.json                    # 即時持倉快照
├── README.md / 策略.md           # 專案說明 + 策略對照
├── requirements.txt / .env       # 依賴 + API key
│
├── config/
│   ├── settings.yaml             # V1 設定（V1 系列現役）
│   ├── settings_loader.py
│   └── r3_strategy.yaml          # ★ R3 參數 source of truth
│
├── core/                         # live trading 核心
│   ├── exchange.py               # 幣安 API 封裝（公開/私有客戶端分離）
│   ├── order_manager.py          # 下單封裝
│   ├── position_manager.py       # 持倉同步（含 3 層 sync 防護）
│   ├── risk_manager.py           # 風控（日虧、連虧、回撤）
│   └── state_store.py            # state.json 持久化
│
├── strategies/
│   ├── base_strategy.py
│   ├── candidates.py             # V1 / V2 系列（保留為對照組）
│   ├── range_breakout.py         # 朋友箱體 v1.6
│   ├── _legacy/                  # ema_crossover, momentum_breakout
│   └── r3/                       # ★ R3 模組
│       ├── config_loader.py      # ✓ 已實作
│       ├── indicators.py         # Sprint 1
│       ├── regime.py             # Sprint 2
│       ├── confirmation.py
│       ├── risk_engine.py
│       ├── trailing.py
│       ├── executor.py
│       ├── trend_pullback.py     # Sprint 3
│       ├── mean_reversion.py     # Sprint 4
│       ├── funding_reversal.py   # Sprint 5
│       └── router.py
│
├── indicators/technical.py       # 技術指標庫
├── scanner/                      # 多幣掃描
├── utils/logger.py
│
├── backtest/
│   ├── data_loader.py / report.py
│   ├── engine_portfolio.py       # 上一代 portfolio 引擎
│   ├── engine_portfolio_v2.py    # ★ V2 引擎（Active/Shadow + RICK 風控）
│   └── _legacy/
│
├── tools/
│   ├── validate_v2.py / validate_cross.py
│   ├── mcpt_portfolio_v2.py / walk_forward_v2.py
│   ├── backtest_top20_strategies.py
│   ├── account/                  # check_account, check_leverage_bracket
│   └── _legacy/
│
├── tests/
│   └── test_r3.py                # ★ Q21–Q29 單元測試（34 cases）
│
├── data/
│   ├── *_1h.csv / *_4h.csv / *_1d.csv
│   ├── *_results.json
│   └── _archive/                 # 歷史 15m + 舊 backtest_trades
│
├── logs/
│   ├── titan.log + 最近 5 天
│   └── archive/                  # 04-04 ~ 04-23
│
├── docs/
│   ├── R3_spec.md                # ★ R3 規格 v1.0（鎖檔）
│   ├── TITAN_v1.3_spec.md
│   └── manus_momentum_funding_report.md
│
└── .claude/commands/
    ├── ship.md                   # /ship — 標準推送流程
    └── spec-lock.md              # /spec-lock — 策略規格鎖定流程
```

---

## 快速開始

### 1. 環境準備

```bash
git clone https://github.com/RayzChang/TITAN.git
cd TITAN

python -m venv .venv
.venv\Scripts\activate         # Windows
pip install -r requirements.txt
```

### 2. 設定 API 金鑰

```bash
cp .env.example .env
```

編輯 `.env`：

```env
BINANCE_DEMO_API_KEY=你的_Demo_API_Key
BINANCE_DEMO_API_SECRET=你的_Demo_API_Secret
```

### 3. 跑 R3 單元測試（驗證 spec ↔ config 一致性）

```bash
.venv/Scripts/python -m pytest tests/test_r3.py -v
# → 11 passed, 23 skipped (skipped 為待 Sprint 1+ 實作的 logic tests)
```

### 4. 跑 V1 / V2 / 箱體回測（現役對照）

```bash
.venv/Scripts/python tools/validate_v2.py     # V2 完整驗證
.venv/Scripts/python tools/validate_cross.py  # V1 vs V1-CROSS-100X 對比
```

### 5. 啟動 V1 機器人（Demo 模式，R3 實作完成前的過渡方案）

```bash
.venv/Scripts/python main.py
```

---

## 歷史策略沿革（Sunset）

| 版本 | 重點 | 歸宿 |
|---|---|---|
| V1.0 | EMA 交叉 + RSI | 歸檔 → `strategies/_legacy/ema_crossover.py` |
| V1.1 | + 趨勢濾網 + 成交量濾網 | MCPT 通過率不及 V1，棄用 |
| V1.2A/B/C | ADX / ATR 變體 | 改善有限，棄用 |
| V1-CROSS-100X | + RICK 風控 | 通過 cross 驗證，保留為對照 |
| V2-SL-075/100/DYN | Aggressive Passive | 待 V2 portfolio 驗證完成 |
| Range Breakout v1.6 | 朋友箱體 + 層疊 | 現役（共存） |
| **R3 v1.0** | **Regime + Risk + Return** | **★ 主路線** |

---

## 開發團隊

| 代號 | 角色 | 職責 |
|------|------|------|
| **RAYZ** | BOSS | 專案擁有者，最終決策 |
| **MIA** | 總指揮 + 策略長 | 統籌全局、策略設計、程式碼整合、規格鎖定 |
| **SAM** | 策略研究員 | 技術指標研究、參數優化 |
| **REX** | 回測工程師 | 歷史回測、L0–L6 驗證 |
| **QA** | 測試工程師 | 單元測試、穩定性測試 |
| **SHIELD** | 風控官 | 風險控管、安全機制 |

外部協作：**RICK**（OpenAI Agent，用於跨模型 cross-validation）、**Manus**（量化驗證對照）。

---

## 免責聲明

> **加密貨幣合約交易具有高度風險。** 使用槓桿交易可能導致超出初始投資的損失。TITAN 僅為自動化交易工具，不構成任何投資建議。過往績效不代表未來表現。使用本程式進行交易之風險由使用者自行承擔。請在充分了解風險後，僅使用可承受損失的資金進行交易。

---

## 授權

本專案採用 [MIT License](LICENSE) 授權。

---

<p align="center">
  <b>TITAN R3</b> — Built by MIA Team, Powered by Discipline<br/>
  <i>不靠槓桿賭運氣，靠 Regime 判斷 + 風險工程站著賺</i>
</p>
