# v0.9 第二次開發：研究擴充與可信度邊界

本輪仍在 research/okx-v09 與 PR #1；所有既有資金與進場合格門檻不變。第一版結果與驗證檔保留於 results/，是當時版本的歷史紀錄；新結果另存 round2_results/，不可用舊 validation.json 為新程式背書。

## Actions 404 修復

未註冊於預設分支的新工作流程不能 workflow_dispatch。改以僅匹配 `research/okx-v09` 及該 v0.9 workflow 自身路徑的 push 觸發。開發程式與資料的提交帶 [skip ci]；單獨改動新 workflow 啟動研究，避免既有 broad-path 歷史 workflow 被觸發。無排程、無推播、contents:read、無部署。沒有修改 master 以便註冊 workflow。

## 樣本不足的來源與擴充設計

1. 預留最長 14 日 adverse funding、雙邊成本及保守結構止損後，許多技術訊號的加權淨 RR 未達 3。
2. 結構 TP1 必須在進場的有利方向；突破已超過前55棒極值時，這套固定目標會拒絕進場。未因樣本不足而改用事後較遠目標。
3. 一個模擬帳戶回撤20%或費用預算剩500即永久停止新倉；資料延長不會讓已停機帳戶恢復。
4. 同策略2筆、全帳戶3筆、2000總風險、日內連虧與流動性限制也排除候選。

新增 raw_candidate_signals、structural_net_rr_pass、各拒絕計數、首次持續停機時間與原因。raw 是當時未持有該幣時產生的候選，不是每根K線全部訊號的普查。

事前固定擴大範圍：2022-01-01 至 2026-01-01 UTC，24 種 USDT 永續合約，1H/4H/1D：BTC ETH SOL XRP ADA DOGE LINK AVAX LTC BCH DOT TRX ETC FIL UNI AAVE ATOM NEAR ALGO XLM SAND MANA ICP CRV。未依新回測報酬挑幣；當前上市狀態與規格仍有存活者偏差，不能稱為歷史全市場或前200。

除60日暖機、60/20/20時間分割及14日embargo外，`--annual-cohorts` 系統性列出2022至2025各年度**獨立假想帳戶**。每個帳戶仍固定10000本金、1000費用預算、原有停機規則，年內不恢復。它們不是同一帳戶翌年自動解鎖；與主要分區重疊，不能把樣本合併當獨立證據。相同固定策略不訓練、不調參；舊觀察區間已被看過，擴充保留集不自稱 pristine。前瞻驗證仍缺少，可信勝率與實盤資格不會因樣本增加而自動通過。

## 全倉與事件

margin.cross_account 使用外部明確供給的帳戶現金、負債、折算擔保品、費用、funding、所有部位標記價格、級距生效/失效時間，計算共享權益、初始保證金、維持保證金＋費用需求、1/3/5/10/20%共同不利跳空，以及模型維持保證金缺口邊界。多空都在不利方向壓力，沒有假裝這是一個可同時觀測的單因子行情。級距按供給的持倉單位區間、整筆費率與deduction，不捏造OKX級距。缺失/過期/未核實/超槓桿直接拒絕。

此為線性USDT的供給級距模型，尚無官方帳戶模式、維持保證金、ADL及清算流程認證，`allow_recommendation` 永遠 False；候選報告實際接入此拒絕結果，不因一個 cross_verified 布林值就宣稱安全。沒有索取或使用帳戶金鑰。5/10/20只限紙上研究，100倍仍拒絕。

verified_events.json 為4份實際HTTP核對成功的Fed官方聲明，使用頁面2pm EDT/EST換算的**發布當下**，沒有把聲明提前到會議或早期日程當時可見；每份附原文URL與核對日期。這是部分示範資料，不是完整FOMC或新聞資料庫。其餘大選、四巫日、專案/監管事件可由同一輸入介面提供，尚未建全庫。事件風險視窗有上下界(最多168小時)，評估可附前一已收盤ATR比例與流動性。已公開重大事件可阻擋紙上新倉；所有win_rate_adjustment均為null。沒有消息不代表沒有風險；不能以技術勝率直接量化事件的勝率增減。

## 統計定義

報表區分勝率、樣本數、計畫加權淨RR、平均實現淨R、實現淨盈虧比(平均獲利/平均虧損絕對值)、PF(總獲利/總虧損)、最大回撤及平均持倉。無虧損或無交易時無法可靠計算比值則null，不以無限大充當績效。


## 重現本輪擴充研究

在 `okx-monitor/backtest`：

```bash
mkdir -p expanded_cache
tar -xzf v09/round2_results/ohlcv.tar.gz -C expanded_cache
python -m v09 --start 2022-01-01T00:00:00Z --annual-cohorts --symbols BTC ETH SOL XRP ADA DOGE LINK AVAX LTC BCH DOT TRX ETC FIL UNI AAVE ATOM NEAR ALGO XLM SAND MANA ICP CRV --cache expanded_cache --instrument-snapshot v09/round2_results/coverage.json --events v09/verified_events.json --output round2_reproduced
python -m v09.summarize round2_reproduced/summary.json
```

同一個 source revision、輸入資料雜湊、合約快照與事件檔可重現。不把2026年的合約規格誤稱為2022年可知的歷史規格：歷史最小單位/跳動/槓桿級距尚未重建，為明示的靜態執行假設，亦阻止實盤資格。匯率32為事前凍結模型假設(有效起點2022-01-01)，非觀測匯率或未來匯率預測。

1D棒的棒內成交先後與確切時間不可得，日內熔斷依保守的棒末退出時間；不能聲稱等同真實台北日內成交帳本。實際人工帳本則使用使用者確認時間。

Wilson區間只作探索性二項比例摘要；同時或高度相關資產並非獨立樣本，尚未做按日期/資產群聚的保守推論，不能僅因n達100、點估計達60%就宣稱可靠。年度重疊研究不會被合併送入可信度判定。


## 實際雲端驗證結果

- 首次解除404阻礙：[40087716537](https://github.com/Jony666/Alert/actions/runs/40087716537)，success，331項測試與原始126情境。
- 最新完整擴充：[40088129495](https://github.com/Jony666/Alert/actions/runs/40088129495)，success，source commit `8abd3a1fe52f91cf4d4bb2f51f57a9bc6f69c9ad`，331項測試（v0.9 104項）與294情境。
- 最新artifact ID `12557323810`，SHA256 `6e1e5a95617b9060aaaa0516a5803a48fad199034b382509ce2dd739c995048b`。已下載後實際逐筆比對：摘要、1942筆情境交易、1203916筆資金曲線、1942筆候選報告，內容均完全相同；不是只比對工作流程綠勾。
- 本地72組資料SHA、1086984根K線、風險/持倉/持倉天數/淨RR逐筆驗證通過。證據在 round2_results/validation.json、cloud-validation.json、unit-tests.txt。
- 雲端成功只證明程式完整執行及結果可重現，沒有任何策略因此成為合格實盤建議。294個情境共1942筆是重疊研究計數，不能宣稱有1942個獨立樣本。

實際master仍為 `9ecaf2743aed0ac956d174067ed003f3d178a183`；全PR相對master只新增v0.9路徑、新測試和新workflow。未修改/合併master，未改v0.7/v0.8，未操作Render或交易API，未啟用推播。後續證據與文件提交使用[skip ci]，其程式源碼與成功雲端source commit相同。
