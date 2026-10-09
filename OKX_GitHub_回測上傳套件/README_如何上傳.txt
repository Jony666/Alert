【OKX GitHub 免費回測工作流程上傳說明】

這是一個全新、獨立的研究工作流程：不會刪除/覆蓋目前 Render 的 scanner.py、render.yaml，且不會送任何交易訊號。

下載 ZIP 解壓縮後，會看到兩個資料夾：
  1) okx-monitor（裡面只有一個新 backtest 資料夾）
  2) .github（裡面是 workflows/okx-backtest.yml）

到你的 https://github.com/Jony666/Alert ，在登入狀態點 Add file -> Upload files。
將上方兩個資料夾「一起」拖進 GitHub Upload files 頁面。確認出現以下路徑後 Commit：
  okx-monitor/backtest/historical_backtest.py
  okx-monitor/backtest/tests.py
  okx-monitor/backtest/requirements.txt
  okx-monitor/backtest/README.md
  .github/workflows/okx-backtest.yml

注意 Mac Finder 可能隱藏 .github 資料夾；在 Finder 按 Command+Shift+. 切換顯示隱藏檔。
請勿把 README_如何上傳.txt 或 ZIP 本身上傳 GitHub。

完成後 GitHub Actions -> OKX Historical Backtest Pilot -> Run workflow。
本 workflow 同時設定 push 觸發，首次上傳時也可能自動執行一次；如果看到已經執行，不必再點一次。

回測可能因 OKX 對 GitHub 美國雲端主機的地域限制而無法取得資料。如果出錯，不得宣稱回測已完成。提供執行紀錄讓 ChatGPT 判斷。
本研究程式沒有交易權限。交易訊號推播維持關閉。
