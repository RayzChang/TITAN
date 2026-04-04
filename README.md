<p align="center">
  <img src="https://img.shields.io/badge/TITAN-v1.0-blue?style=for-the-badge&logo=bitcoin&logoColor=white" alt="TITAN v1.0"/>
  <img src="https://img.shields.io/badge/Python-3.14-green?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.14"/>
  <img src="https://img.shields.io/badge/Binance-Futures-yellow?style=for-the-badge&logo=binance&logoColor=white" alt="Binance Futures"/>
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

## 簡介

**TITAN** 是一款全自動化的幣安永續合約交易機器人，基於技術指標策略（EMA 交叉 + RSI 過濾）執行交易，搭配嚴格的風控系統與複利機制，追求長期穩定正向收益。

核心理念：**紀律 > 預測，風控 > 獲利，複利 > 重倉**

---

## 特色功能

| 功能 | 說明 |
|------|------|
| **自動交易** | 24/7 全自動監控市場、產生訊號、執行下單 |
| **動態幣種掃描** | 自動追蹤市值前 20 大幣種，找出最佳交易機會 |
| **多重策略支援** | EMA 交叉、布林帶均值回歸、網格交易（可擴充） |
| **嚴格風控** | 止損止盈、日損限制、連續虧損暫停、帳戶回撤保護 |
| **複利機制** | 獲利自動滾入本金，動態調整倉位大小 |
| **測試網模式** | 支援幣安 Testnet，用假錢完整驗證策略 |
| **繁體中文日誌** | 所有日誌與報告均為繁體中文，清晰易讀 |
| **每日績效報告** | 自動產生每日損益、勝率、交易明細報告 |

---

## 專案結構

```
TITAN/
├── main.py                       # 程式進入點
├── requirements.txt              # Python 依賴套件
├── .env.example                  # 環境變數範本
│
├── config/
│   ├── settings.yaml             # 交易參數設定（繁體中文註解）
│   └── settings_loader.py        # 設定檔載入與驗證
│
├── scanner/
│   ├── market_scanner.py         # 市值前 20 大幣種動態掃描
│   └── symbol_filter.py          # 篩選幣安可交易合約幣種
│
├── core/
│   ├── exchange.py               # 交易所連線（ccxt 封裝）
│   ├── order_manager.py          # 下單 / 改單 / 取消管理
│   ├── position_manager.py       # 倉位追蹤與損益計算
│   └── risk_manager.py           # 風控引擎
│
├── strategies/
│   ├── base_strategy.py          # 策略抽象基類
│   ├── ema_crossover.py          # EMA 交叉 + RSI 過濾策略
│   ├── bollinger_reversion.py    # 布林帶均值回歸策略
│   └── grid_strategy.py          # 網格交易策略
│
├── indicators/
│   └── technical.py              # 技術指標計算（EMA/RSI/BB/MACD/ATR）
│
├── backtest/
│   ├── engine.py                 # 回測引擎
│   ├── data_loader.py            # 歷史 K 線數據載入
│   └── report.py                 # 回測績效報告
│
├── utils/
│   ├── logger.py                 # 日誌系統
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
# 複製專案
git clone https://github.com/RayzChang/TITAN.git
cd TITAN

# 建立虛擬環境
python -m venv .venv

# 啟動虛擬環境（Windows）
.venv\Scripts\activate

# 安裝依賴
pip install -r requirements.txt
```

### 2. 設定 API 金鑰

```bash
# 複製環境變數範本
cp .env.example .env
```

編輯 `.env` 填入你的幣安 API 金鑰：

```env
BINANCE_API_KEY=你的_API_Key
BINANCE_API_SECRET=你的_API_Secret
BINANCE_TESTNET_API_KEY=你的測試網_API_Key
BINANCE_TESTNET_API_SECRET=你的測試網_API_Secret
```

### 3. 調整交易參數

編輯 `config/settings.yaml`，所有參數皆有繁體中文註解。

### 4. 啟動機器人

```bash
# 測試網模式（預設）
python main.py

# 正式交易模式（請確認風險！）
# 在 settings.yaml 中將 mode 改為 "live"
python main.py
```

---

## 交易參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| 保證金模式 | Cross（全倉） | 全倉模式，共享保證金 |
| 槓桿 | 20x | 固定槓桿倍數 |
| 單筆倉位 | 總資金 10% | 每筆交易使用的保證金比例 |
| 止損 | 1.5% | 價格反向 1.5% 自動平倉 |
| 止盈 | 3.0% | 價格正向 3.0% 自動平倉 |
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

## 策略說明

### EMA 交叉 + RSI 過濾（主策略）

**原理：** 利用快慢均線交叉捕捉趨勢轉折，搭配 RSI 過濾假訊號。

```
做多條件：EMA-9 上穿 EMA-21 且 RSI 介於 30-70
做空條件：EMA-9 下穿 EMA-21 且 RSI 介於 30-70
止損設定：入場價 ± 1.5%
止盈設定：入場價 ± 3.0%
```

| 參數 | 數值 |
|------|------|
| 快線 EMA | 9 週期 |
| 慢線 EMA | 21 週期 |
| RSI 週期 | 14 |
| K 線週期 | 15 分鐘 |

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

TITAN 採用 **每 3 天一次迭代** 的敏捷開發模式：

| 版本 | 週期 | 重點 |
|------|------|------|
| v1.0 | Day 1-3 | 基礎建設 + 策略初版 |
| v1.1 | Day 4-6 | 回測分析、參數第一輪優化 |
| v1.2 | Day 7-9 | 風控精進、回測再驗證 |
| v1.3 | Day 10-12 | 測試網上線 |
| v1.4 | Day 13-15 | 測試網數據分析、策略微調 |
| v1.5 | Day 16-18 | 穩定性強化 |
| v1.6 | Day 19-21 | 正式上線準備 |
| v1.7 | Day 22-24 | 實盤分析、策略微調 |
| v1.8 | Day 25-27 | 績效優化 |
| v1.9 | Day 28-30 | 最終評估、全額運行 |

---

## 技術棧

| 項目 | 技術 |
|------|------|
| 語言 | Python 3.14 |
| 交易所 API | [ccxt](https://github.com/ccxt/ccxt) |
| 技術指標 | [ta](https://github.com/bukosabino/ta) |
| 數據處理 | pandas, numpy |
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
