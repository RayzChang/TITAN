# /spec-lock — 策略規格鎖定工作流

當 BOSS 想把一個交易策略概念落實成 production-grade 規格時使用。
**這是 R3 v1.0 (2026-04-30) 鎖檔過程萃取出來的工作流，未來新策略走同一套。**

## 何時觸發

- BOSS 說「鎖規格」、「lock spec」、「正式寫進規格」、「進入工程實作階段」
- BOSS 已對某策略討論到一定深度，給出整套設計（含進場/出場/風控）
- BOSS 把 deep research / 外部 agent 報告交給 MIA 並要求落地

## 何時**不**該觸發

- 還在概念探索階段（用 Plan / 對話足矣）
- 只是要修現有策略某個參數（直接改 config 即可）
- 只是要試回測一個變體（用 tools/_legacy 範本）

---

## 核心原則

> **任何策略歧義都要在進工程前釘死，否則開發中會被反覆問同一題。**

1. **先 ambiguity hunt，後鎖檔**：先把所有「附近」、「重新上彎」、「中性」這種模糊詞找出來
2. **每題附 MIA default**：BOSS 用「全接受 default」一句話打發；要改才需多打字
3. **鎖檔不可逆**：版本鎖死後變更必須開新版號
4. **config ≡ spec**：兩邊任一改動都要同步，測試自動驗證
5. **Fail 時不偷偷調參**：驗證 fail → 回報失敗，禁止改 spec/config 讓它通過

---

## 工作流程

### Phase 0 — 接收策略草稿

BOSS 通常會丟一份完整設計（像 R3 那種）。MIA 要：

1. 先讀完整份
2. 給整體評分（vs. 過去策略）
3. 點出**最棒的設計決策**（讓 BOSS 知道這次有進步）
4. 進入 Phase 1

### Phase 1 — Iterative Q&A（最關鍵）

把 BOSS 的草稿在腦中跑一遍 **end-to-end 程式流程**：

```
訊號產生 → 訊號確認 → 進場執行 → 持倉監控 → 風控檢查
   → 移動停利 → 出場 → 連虧計數 → cooldown → 下一輪
```

每個環節找出**模糊點**：

#### 訊號層級 ambiguity 模板
- 「附近」距離多少？用 ATR 倍數還是 % 還是 tick？
- 「重新上彎」精確定義（是 RSI[i]>RSI[i-1]？還是過去 N 根 + 越過閾值？）
- 「過熱」/「中性」門檻是 z-score 多少？
- 多時框時序對齊（1H 訊號後 5M 確認的有效窗口幾根？）

#### 風控層級 ambiguity 模板
- 連虧計數跨日清不清？
- risk_multiplier 恢復條件是什麼？
- 部分平倉後剩餘部位的 risk 算 0 嗎？
- BTC + ETH 同方向的 correlation haircut 怎麼分配？
- Equity 基準算 unrealized 嗎？

#### 多策略交互 ambiguity 模板
- Regime 切換時既有持倉怎麼辦？
- 同幣不同策略反向訊號出現時優先誰？
- 副策略遇反向倉位是減倉、等待、還是強平？

#### 執行層級 ambiguity 模板
- Maker timeout 後重掛還是放棄？
- 部分成交怎麼處理？
- Taker fallback 條件是什麼？
- Trailing trigger 用 close 還是 intrabar hit？

#### 回測 / 驗證層級 ambiguity 模板
- Warmup 期間能交易嗎？
- Funding 結算 PnL 計入 daily loss 嗎？
- 樣本切分 IS/OOS 如何切？
- L4 Bonferroni 的 m 怎麼算？

**重要**：每題都附「**MIA default**」附理由。BOSS 想接受 → 一句話 OK；想改 → 直接點題。

#### 分批節奏

不要一次丟 30 題。用 **3-2-1 節奏**：
- 第 1 輪：12–15 題（最核心訊號 + 風控）
- 第 2 輪：5–8 題（補強第 1 輪後浮現的細節）
- 第 3 輪：3–5 題（訊號核心 + 邊界）
- BOSS 拍板後 MIA 主動問「**還有問題嗎？所有問題都解決了嗎？**」並老實回答

### Phase 2 — 鎖檔（4 個檔案）

#### 1. `docs/<STRATEGY>_spec.md`

**必含章節**：
- 開頭 metadata 表（版本、鎖檔日期、決策者、狀態）
- 命名意義 / 設計理念
- 整體架構圖（mermaid 或 ASCII）
- 交易標的（首發 + 後續階段）
- 時間框
- Regime / 策略路由
- 主策略 1, 2, ... 進場條件
- 副策略
- 移動停利模式
- 風險管理（含三檔風險 profile）
- Funding 處理
- Pivot / 結構停損
- 驗證體系 L0–L6
- 部署流程
- **Q1–QN 決策歷史對照表**（必須有，未來 review 用）
- 工程紀律
- 待後續 review 清單

#### 2. `config/<strategy>.yaml`

**必含元素**：
- 開頭 `version` + `spec_ref` 指向 `docs/<STRATEGY>_spec.md`
- 14+ 區塊**對應 spec 章節**
- 三檔風險 profile（保守 / 成熟 / 激進，首發強制保守）
- `engineering_discipline` 區塊明列鐵律
- **無 magic number 散落策略邏輯**

#### 3. `strategies/<strategy>/` 模組骨架

**必含**：
- `__init__.py`（標 spec 版本 + 路徑）
- `config_loader.py`（**必須立即實作完成 + smoke test**）— 後續所有模組從這裡取參數
- 每個策略邏輯模組：`indicators.py`, `regime.py`, `confirmation.py`, `risk_engine.py`, `trailing.py`, `executor.py`, `<strategy>.py`, `router.py`
- 每個 stub 標 `# TODO[Sprint-N]` + 對應 spec 章節 + 預估工時

#### 4. `tests/test_<strategy>.py`

**必含**：
- 每個 Q 一個 `TestQ<N><Topic>` class
- 每個 class 至少：
  - `test_config_values_match_spec`（驗證 yaml 對得上 spec）
  - 1 個 happy path（標 `pytest.skip("TODO[Sprint-N]")`）
  - 1 個 edge case（同上）
- **特別寫「禁止做某事」的 test**（例如 R3 的 `test_does_not_use_breakout_signal` for MR）

### Phase 3 — 紀律驗收

```bash
.venv/Scripts/python -m pytest tests/test_<strategy>.py -v
```

**通過標準**：
- 所有 `test_config_values_match_spec` PASS
- 邏輯 tests 全部 SKIPPED（待 Sprint 實作）
- 0 ERROR、0 FAILED

如有 FAILED → spec 與 config 不一致，**立刻修正**。

### Phase 4 — 鎖檔聲明

跑 git status 確認改動，然後分 2 個 commit：

```bash
# 若有累積未提交的舊工作
git add <pre-existing-changes>
git commit -m "chore: <pre-existing-summary>"

# 鎖檔本體
git add docs/<STRATEGY>_spec.md
git add config/<strategy>.yaml
git add strategies/<strategy>/
git add tests/test_<strategy>.py
git add README.md
git commit -m "feat: <STRATEGY> v1.0 spec lock"
```

最後**主動跟 BOSS 提交一份「決策一致性聲明」**：列出哪些紅線一旦觸碰要 stop。

---

## 給 MIA 的執行 checklist

- [ ] 讀完整份草稿，給整體評分
- [ ] 點出最棒的 3-4 個設計決策
- [ ] 第 1 輪 ambiguity（12-15 題）+ MIA defaults
- [ ] 第 2 輪 ambiguity（5-8 題）+ MIA defaults
- [ ] 第 3 輪 ambiguity（3-5 題）+ MIA defaults
- [ ] 主動問「所有問題都解決了嗎？」
- [ ] 寫 `docs/<STRATEGY>_spec.md` — 必含 Q 對照表
- [ ] 寫 `config/<strategy>.yaml` — 含 engineering_discipline 區塊
- [ ] 建 `strategies/<strategy>/` 骨架 — config_loader 立即實作
- [ ] 寫 `tests/test_<strategy>.py` — 每 Q 一個 class
- [ ] 跑 pytest 驗證 config-spec consistency
- [ ] 更新 `README.md` 反映新狀態
- [ ] 提交 2 個 commit
- [ ] 提交決策一致性聲明（哪些紅線觸碰要 stop）

---

## 範例：R3 v1.0 (2026-04-30)

| 項目 | 值 |
|---|---|
| 總題數 | 29 題（Q1–Q29）+ Tier 3 default 4 題 |
| Q&A 輪數 | 3 輪 |
| spec 章節 | 14 |
| config 區塊 | 14 |
| 模組數 | 11 |
| 測試 case | 34 |
| 規格檔 | `docs/R3_spec.md` |
| 參數檔 | `config/r3_strategy.yaml` |
| 測試檔 | `tests/test_r3.py` |

---

## 注意事項

- **不要在 Phase 1 結束前寫任何程式碼**。模糊規格寫出來的 code 會讓 spec lock 失去意義。
- **不要把 ambiguity hunt 委派給 sub-agent**。MIA 親自走流程才能看出 BOSS 設計的潛在矛盾。
- **MIA default 必須有理由**。「我建議 X」要附「因為 Y」，否則 BOSS 沒辦法判斷。
- **不要省略 Q 對照表**。未來 review 時，「為什麼當初這樣定？」必須查得到。
- **每次 spec 變更必須開新版號**。v1.0 → v1.1 是參數調整；v1.0 → v2.0 是架構變更。
