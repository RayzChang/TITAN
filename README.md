<p align="center">
  <img src="https://img.shields.io/badge/TITAN-v1.1-blue?style=for-the-badge&logo=bitcoin&logoColor=white" alt="TITAN v1.1"/>
  <img src="https://img.shields.io/badge/Python-3.14-green?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.14"/>
  <img src="https://img.shields.io/badge/Binance-Futures-yellow?style=for-the-badge&logo=binance&logoColor=white" alt="Binance Futures"/>
  <img src="https://img.shields.io/badge/Phase-3%20Live-brightgreen?style=for-the-badge" alt="Phase 3 Live"/>
  <img src="https://img.shields.io/badge/License-MIT-red?style=for-the-badge" alt="MIT License"/>
</p>

<h1 align="center">TITAN v1</h1>
<h3 align="center">Trading Intelligent Tactical Automation Network</h3>
<p align="center"><b>交易智能戰術自動化網絡</b></p>

<p align="center">
  全自動幣安 USDT-M 合約交易機器人<br/>
  專為紀律型交易者打造 — 嚴守止盈止損，複利滾倉，穩健獲利
</p>

---

## 目前狀態

| 項目 | 狀態 |
|------|------|
| Phase 1 基礎建設 | ✅ 完成 |
| Phase 2 策略 + 回測 | ✅ 完成 |
| Phase 3 Demo 實盤模擬 | ✅ 完成（交易迴圈已上線） |
| Phase 4 正式上線 | ⏳ 待定 |

**當前策略版本：V1.1**（EMA 交叉 + RSI + 趨勢濾網 + 成交量濾網）

---

## 簡介

**TITAN** 是一款全自動化的幣安永續合約交易機器人，基於技術指標策略（EMA 交叉 + RSI 過濾）執行交易，搭配嚴格的風控系統與複利機制，追求長期穩定正向收益。

核心理念：**紀律 > 預測，風控 > 獲利，複利 > 重倉**

---

## 特色功能

| 功能 | 說明 |
|------|------|
| **自動交易** | 24/7 全自動監控市場、產生訊號、執行下單 |
| **市值動態掃描** | 透過 CoinGecko 追蹤真實市值前 20 大幣種，交叉比對幣安合約可用性 |
| **多重過濾策略** | EMA 交叉 + RSI + 長期趨勢濾網（EMA-100）+ 成交量確認 |
| **嚴格風控** | 止損止盈、日損限制、連續虧損暫停、帳戶回撤保護 |
| **複利機制** | `compound: true` 時每次以最新帳戶餘額計算倉位，獲利自動滾入本金 |
| **倉位監控** | PositionManager 即時追蹤持倉，自動偵測 SL/TP 觸發並記錄損益 |
| **每日報告** | UTC 00:01 自動輸出損益/勝率/逐筆明細（繁體中文格式） |
| **優雅關閉** | Ctrl+C 自動強制平倉 → 輸出最終報告 → 安全退出 |
| **Demo 模式** | 支援幣安 Demo Trading，用真實行情完整驗證策略 |
| **事件驅動回測** | 嚴格防止 lookahead bias，訊號在 K 線 i，成交在 K 線 i+1 開盤價 |

---

## 策略說明（V1.1 當前版本）

### EMA 交叉 + RSI + 趨勢濾網 + 成交量濾網

**進場邏輯：**

```
做多條件（全部需滿足）：
  1. EMA-9 上穿 EMA-21（Golden Cross）
  2. RSI 介於 35-65（縮緊，避免追高追低）
  3. 收盤價 > EMA-100（確認大方向向上）
  4. 本根成交量 > 20根均量 × 1.2（量能確認突破）

做空條件（全部需滿足）：
  1. EMA-9 下穿 EMA-21（Death Cross）
  2. RSI 介於 35-65
  3. 收盤價 < EMA-100（確認大方向向下）
  4. 本根成交量 > 20根均量 × 1.2
```

**止損止盈：**

```
止損：入場價 ± 1.5%（底板，ATR 模式只放大不縮小）
止盈：入場價 ± 3.0%（底板，報酬風險比 2:1）
```

**V1.1 相較 V1.0 改動：**
- RSI 閾值縮緊：70/30 → 65/35（減少假突破）
- 新增 EMA-100 趨勢濾網（只順大方向交易）
- 新增成交量濾網（量能不足不進場）

| 參數 | 數值 |
|------|------|
| 快線 EMA | 9 週期 |
| 慢線 EMA | 21 週期 |
| 趨勢 EMA | 100 週期 |
| RSI 週期 | 14（閾值 35-65） |
| 成交量均線 | 20 週期（需超過 1.2x） |
| K 線週期 | 15 分鐘 |

---

## 回測結果（V1.1 × 市值前 20 × 30 天）

> 回測區間：2026 年 3 月 - 4 月（BTC 從 $88K 下跌至 $74K，強烈熊市）

| 幣種 | 報酬率 | 勝率 | 交易數 |
|------|--------|------|--------|
| XLM | +21.68% | — | — |
| SUI | +17.32% | — | — |
| HBAR | +14.79% | — | — |
| BNB | +4.90% | — | — |
| BTC | +4.13% | — | — |
| LINK | +1.30% | — | — |
| *(13 others)* | 負報酬 | — | — |

```
正向獲利幣種：7 / 20
30 天總交易次數：~366 筆（平均每日 12.2 筆）
平均報酬率：-5.50%（熊市環境）
```

> **注意：** 回測期間為近期最差行情，EMA 交叉為落後指標，在急跌行情中較不利。策略框架健全，待中性/多頭行情驗證。

---

## 交易參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| 保證金模式 | Cross（全倉） | 全倉模式，共享保證金 |
| 槓桿 | 20x | 固定槓桿倍數 |
| 單筆倉位 | 總資金 10% | 每筆交易使用的保證金比例（約 $500） |
| 止損 | 1.5% | 價格反向 1.5% 自動平倉（約 $150 單筆虧損） |
| 止盈 | 3.0% | 價格正向 3.0% 自動平倉（約 $300 單筆獲利） |
| 報酬風險比 | 2:1 | 止盈為止損的 2 倍 |
| 最大同時持倉 | 3 | 最多同時持有 3 個倉位 |
| 每日最大交易 | 20 次 | 超過則當日停止交易 |
| 複利 | 開啟 | 獲利自動滾入本金 |

---

## 風控系統

TITAN 內建多層風控保護機制：

```
第 1 層 ─ 單筆止損          價格反向 1.5% 自動平倉
第 2 層 ─ 日損限制          單日虧損達 5% 停止所有交易
第 3 層 ─ 連續虧損暫停      連續 3 筆虧損 → 暫停交易 1 小時
第 4 層 ─ 帳戶回撤保護      帳戶從高點回撤 20% → 停止所有交易
第 5 層 ─ 異常行情跳過      單根 K 線波動 >5% → 不進場
```

---

## 專案結構

```
TITAN/
├── main.py                       # 程式進入點
├── run_backtest.py               # 全市值前 20 回測腳本
├── requirements.txt              # Python 依賴套件
├── .env.example                  # 環境變數範本
│
├── config/
│   ├── settings.yaml             # 交易參數設定（繁體中文註解）
│   └── settings_loader.py        # 設定檔載入與驗證
│
├── scanner/
│   ├── market_scanner.py         # 市值前 20 大幣種動態掃描（CoinGecko）
│   └── symbol_filter.py          # 篩選幣安可交易合約幣種
│
├── core/
│   ├── exchange.py               # 交易所連線（ccxt 封裝）
│   ├── order_manager.py          # 下單 / 改單 / 取消管理
│   ├── position_manager.py       # 倉位追蹤與損益計算
│   └── risk_manager.py           # 風控引擎（多層保護）
│
├── strategies/
│   ├── base_strategy.py          # 策略抽象基類
│   └── ema_crossover.py          # EMA 交叉 + RSI + 趨勢 + 成交量策略（V1.1）
│
├── indicators/
│   └── technical.py              # 技術指標計算（EMA/RSI/BB/MACD/ATR）
│
├── backtest/
│   ├── engine.py                 # 事件驅動回測引擎（防 lookahead bias）
│   ├── data_loader.py            # 歷史 K 線數據載入與快取
│   └── report.py                 # 回測績效報告產生
│
├── utils/
│   ├── logger.py                 # 日誌系統（繁體中文）
│   ├── notifier.py               # 通知推播（Telegram/Line）
│   └── helpers.py                # 工具函式
│
├── data/                         # 歷史數據快取
└── logs/                         # 運行日誌
```

---

## 快速開始

### 1. 環境準備

```bash
git clone https://github.com/RayzChang/TITAN.git
cd TITAN

python -m venv .venv
.venv\Scripts\activate        # Windows
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

### 3. 調整交易參數

編輯 `config/settings.yaml`，所有參數皆有繁體中文註解。

### 4. 執行回測

```bash
python run_backtest.py
```

### 5. 啟動機器人（Demo 模式）

```bash
# settings.yaml 中 mode: "testnet"（預設）
python main.py
```

---

## 複利效果預估

以每日淨收益 1% 為例：

```
Day 1   ─  $5,000
Day 7   ─  $5,355   (+7.1%)
Day 30  ─  $6,739   (+34.8%)
Day 90  ─  $12,135  (+142.7%)
Day 180 ─  $29,460  (+489.2%)
```

> 複利的力量在於時間。紀律執行，讓利潤自己成長。

---

## 迭代規劃

| 版本 | 週期 | 重點 | 狀態 |
|------|------|------|------|
| V1.0 | Day 1-3 | 基礎建設 + 策略初版 | ✅ |
| V1.1 | Day 4-6 | 回測分析、策略第一輪優化（趨勢+成交量濾網） | ✅ |
| V1.2 | Day 7-9 | ATR 動態止損實驗、回測再驗證 | ✅ |
| V1.3 | Day 10-12 | Demo 完整交易迴圈上線（倉位管理、複利、每日報告） | ✅ |
| V1.4 | Day 13-15 | Demo 數據分析、策略微調 | 🔄 |
| V1.5+ | Day 16+ | 穩定性強化、正式上線準備 | ⏳ |

---

## 技術棧

| 項目 | 技術 |
|------|------|
| 語言 | Python 3.14 |
| 交易所 API | [ccxt](https://github.com/ccxt/ccxt) |
| 技術指標 | [ta](https://github.com/bukosabino/ta) |
| 數據處理 | pandas, numpy |
| 市值排名 | CoinGecko Free API |
| 設定管理 | PyYAML |
| 排程 | APScheduler |
| 環境變數 | python-dotenv |

---

## 開發團隊

| 代號 | 角色 | 職責 |
|------|------|------|
| **RAYZ** | BOSS | 專案擁有者，最終決策 |
| **MIA** | 總指揮 + 策略長 | 統籌全局、策略設計、程式碼整合 |
| **SAM** | 策略研究員 | 技術指標研究、參數優化 |
| **REX** | 回測工程師 | 歷史回測、績效報告 |
| **QA** | 測試工程師 | 品質保證、穩定性測試 |
| **SHIELD** | 風控官 | 風險控管、安全機制 |

---

## 免責聲明

> **加密貨幣合約交易具有高度風險。** 使用槓桿交易可能導致超出初始投資的損失。TITAN 僅為自動化交易工具，不構成任何投資建議。過往績效不代表未來表現。使用本程式進行交易之風險由使用者自行承擔。請在充分了解風險後，僅使用可承受損失的資金進行交易。

---

## 授權

本專案採用 [MIT License](LICENSE) 授權。

---

<p align="center">
  <b>TITAN v1</b> — Built by MIA Team, Powered by Discipline<br/>
  <i>紀律是獲利的基石，複利是時間的禮物</i>
</p>
