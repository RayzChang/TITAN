# /ship — TITAN 標準推送流程

每次完成功能開發後，執行此流程確保 README 同步更新再推送。

## 執行步驟

1. **確認本次變動內容**
   - 執行 `git diff` 和 `git status` 了解所有變動
   - 判斷屬於哪個 Phase / 版本（V1.x）

2. **更新 README.md**
   需要同步更新的項目：
   - 頂部 badge（版本號、Phase 狀態）
   - `## 目前狀態` 表格中對應 Phase 的狀態（🔄 進行中 → ✅ 完成）
   - `## 特色功能` 若有新增功能則補上
   - `## 迭代規劃` 對應版本標記完成（✅），下一版設為進行中（🔄）
   - 若有新的回測結果，更新 `## 回測結果` 區塊

3. **Commit 功能變動**（不含 README）
   ```
   git add <功能相關檔案>
   git commit -m "feat/fix/refactor: <描述>"
   ```

4. **Commit README 更新**
   ```
   git add README.md
   git commit -m "docs: 更新 README 反映 <版本/功能> 狀態"
   ```

5. **Push 到 GitHub**
   ```
   git push origin master
   ```

## 注意事項
- README 更新是必要步驟，**絕對不能省略**（BOSS 已糾正過一次）
- 功能 commit 與 README commit 分開，保持 git log 清晰
- CC 已加入 exclude list，不需要特別處理
- 每次 commit message 結尾加上 Co-Authored-By
