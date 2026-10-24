# 第五階段 v0.9.5：凍結前瞻 PAPER、跨執行狀態與稽核

沿用 research/okx-v09 / PR #1。前四階段386項測試、732情境與資料沒有改寫；本階段不是重跑歷史回測冒充前瞻。新程式只在forward/及新workflow，不改Render、v0.7/v0.8、推播、交易API或預設分支。

## 固定策略與准入

只追蹤 VOLATILITY_CONTRACTION_BREAKOUT（內部VOL_SQUEEZE）及 SUPPORT_RESISTANCE_REVERSAL（內部SR_REVERSAL）。沿用前階段原始固定訊號/技術止損/TP1與TP2各半/下一棒2ATR與進場保護/結構失效/3日時間退出，不挑第四階段最高報酬退出。1H/4H/UTC 1D，最長14日硬規範保留；分析週期不是持倉時間。

registry/freeze.json由已提交的實際程式SHA產生，包含兩個strategy_id、依程式和全部設定產生的strategy_version、config_hash、code_commit_sha、freeze_timestamp_utc、完整model_hashes與固定30合約範圍。research_start_timestamp_utc在第一次真實初始化完成時才寫入狀態，不能回溯。版本/設定改變即不同study_id，舊狀態不能混用；不支援默默覆蓋舊研究版本。

工作流程先讀研究分支的freeze/操作用anchor，再checkout精確凍結commit到另一目錄，執行時驗證git HEAD與全部模型SHA；後續文件/證據提交不改實際模型。bootstrap anchor屬操作稽核資料，與策略設定分離。

**未驗證策略不因PAPER名稱自動取得合格進場。** 可信樣本≥100、勝率95%下界≥60%、獨立/乾淨或前瞻證據、官方全倉、新聞覆蓋、加權扣成本淨RR≥3及全部資金gate都要通過。當前freeze quality n=0代表未安裝任何合格的可信證據，不否認歷史10筆/3筆；歷史研究不能寫入前瞻交易帳本。兩套目前仍未驗證，故真實前瞻成交可能為0，不能為交付放寬門檻。非零交易生命週期另以明示合成測試驗證，不假造市場績效。

## 真實資料與時間因果

只GET公開time/instruments/history-candles/mark-price/funding-rate-history，不使用任何OKX金鑰。固定30種目前可交易USDT線性永續，策略先定再按訊號強度/歷史成交額排序。範圍非全市場，上市/下市全集仍有限。每次先恢復驗證狀態，再取真實公開資料。

210日滾動暖機，初始90組約195300根；後續用Artifact內原始JSON快取加最新公開頁，不重新下載全部歷史。confirm=1、完整對齊、無缺棒/倒退、OHLC/volume有限有效、cursor及所有序列SHA；已處理棒若被交易所修訂則停止而非重寫結果。相同時點須覆蓋全部90組，抓取中跨時間邊界會增量重取；最新已確認close必須對齊交易所當下已完成邊界。標記價有獨立時間與時效檢查。缺失/過期/API失敗/規格變更會保留原合法狀態，失敗診斷與合法state Artifact分開。

第一次只設最新已確認水位，候選/待成交/成交/平倉均空，不回填暖機歷史。之後既存訂單與部位可依預先持久化規則處理新棒；新訊號只取本次實際觀察到的最新新收盤，離線期間較早訊號記MISSED_SIGNAL_BARS，不補造過去訂單。

待成交從訊號「觀察當下之後的下一個完整分析棒開盤」起才可生效，且恢復來源Artifact建立時間必須早於生效時間。看到13:00收盤後13:05才觀察，不會假裝13:00已進場。1D因此可能等待近一日；這是明示保守延遲模型，不等同即時下單。下一次才看到完整未來棒，可依前次已持久化條件回放該棒，決策只用開盤及當時已知資訊，之後才處理高低點。同時各市場開盤標記先更新，當前最新mark只在過去事件處理完後用於當下決策，避免未來mark影響較早開盤。

固定預設CONDITIONAL_MARKET_NEXT_FULL_OPEN：不利滑價0.05%、0.5%事前入場區間、開盤重核計畫RR/止損及原風險預算，超出即取消；這是條件式紙上進場假設，不聲稱真實無條件市價單總能撤銷或成交。LIMIT_STRICT_CROSS_PARTIAL實作供獨立新凍結版本使用，當前帳本不動態切換：僅touch不成交，必須嚴格穿價，每棒最多原量50%、受成交額參與率/lot約束，3棒到期；部分退出後取消尚未進場餘量。OHLC模型不證明真實排隊或成交。

同棒停損優先、跳空較差價格，TP1後只在下一棒提升保護/2ATR追蹤，超時3日及原始結構失效退出。funding取已公開歷史率的絕對值乘入場名義額作保守扣款，雙向均不假設收取credit、同棒可能已提前退出仍扣全棒funding；這不是實際funding時點標記價計費，屬明示未驗證成本假設。預留計畫funding仍為14日0.03%/8h界線。所有每筆費用、滑價及部分出場分開記錄，未平浮盈不算已平倉績效。

## 資金與帳本隔離

PAPER_FORWARD專用10000本金與1000費用預算，非使用者真實資產。單筆計畫1000、總計畫2000、同策略2/全策略3，待成交預留也占風險與部位，不重複計算部分進場的同一候選。全帳戶共享book，跨週期也共同受限；不把三週期當三個獨立本金。

沿用risk.State的20%起始/高水位持續停機、費用≤500停機；台北同日連虧兩筆另加鎖定旗標，既有部位後來盈利也不自動解除當日停機。未經使用者逐筆特批，不自動第三筆；原始階梯公式保留，但PAPER盈虧不會寫成已核實真實本金，單筆不因浮盈/紙上盈利自動調升。真實帳本與歷史回測帳本不能被此restore接受，沒有PAPER轉ACTUAL接口。

未知新聞不當作無新聞：目前無全面且可核實時效的當前事件feed，記UNKNOWN_COVERAGE並阻擋新增進場；維持原有事件時間邏輯，不虛構勝率修正。全倉與可信勝率也未驗證，預設拒絕；5倍只是假設紙上額度上限，非安全證明。NT1000預算不是跳空實損保證。

## 跨次持久化

每份Artifact有完整累計state、候選、pending、positions、部分成交、TP狀態、closed、cash、費用/funding、equity、daily/global halt、last_success/run/revision、frame cursors、freeze及journal。事件用study/種類/業務key穩定ID、順序/hash-chain，payload深拷貝避免後續狀態修改舊事件。所有交易都帶strategy_id/version/config/source/start/freeze欄位。

原子bundle以同目錄temp寫完並fsync再rename，已有bundle不覆寫；本機flock和Actions固定concurrency group、cancel=false防併發。manifest記所有檔案SHA、state hash、parent run/artifact/hash/revision。恢復檢查全部文件、版本、genesis anchor、事件鏈、風險、現金對帳及7日狀態時效；過期停止，不偷偷重置。重複行情只增加操作稽核revision/run事件，不增加交易/平倉/費用/績效或equity列。

restore選「本workflow/本研究分支最新成功run」的匹配state Artifact；該Artifact遺失/過期/損毀則失敗，不回退舊run或建立新帳。失敗run的未提交半成品不視為狀態；失敗診斷Artifact不會被restore消費。首次initialize也要求沒有既有執行史或genesis anchor；已提交研究分支的anchor能阻止刪除執行歷史後意外重置。不得修改anchor繞過金融停機。

90天Artifact是暫存，不是永久資料庫。每次完整累計交易/事件/曲線可承接較舊歷史，但原始行情快取滾動210日，舊執行原始輸入仍可能過期。應定期由使用者下載整份state Artifact及manifest，保存完整不可變外部封存與校驗值。尚無已授權永久目的地，不新增帳戶/服務、不自動提交帳本到master；連最新合法狀態也遺失時停止，需人工從合法匯出恢復，而非清零。

## 工作流程與操作

`.github/workflows/okx-v09-forward-paper.yml`獨立於第四階段與任何正式流程。contents:read/actions:read、無schedule、無推播/部署/交易權限；90天完整state Artifact僅在原子成功後upload，failure診斷另名。先control checkout讀freeze/anchor，再checkout精確source SHA，執行固定版本測試、restore、行情/狀態、報告及upload。

首次研究分支push僅用於註冊新workflow；初始化成功後提交genesis anchor並改預設resume。優先實際驗證workflow_dispatch：

```bash
gh workflow run okx-v09-forward-paper.yml --repo Jony666/Alert --ref research/okx-v09 -f mode=resume
gh workflow run okx-v09-forward-paper.yml --repo Jony666/Alert --ref research/okx-v09 -f mode=verify_replay
```

verify_replay是真正從前次雲端Artifact恢復，但重用同一份真實已保存行情（不是新的市場觀察）；驗證financial hash完全相同，並在臨時副本模擬缺失/損毀安全拒絕，不刪改來源Artifact。與非零合成部位單元測試分開報告。

GitHub schedule通常只從預設分支生效。本階段不新增任何schedule，更不為排程合併master。未來可先評估每小時收盤後5–10分鐘檢查；正式啟動仍需使用者另外授權可行的排程/儲存方案。手動研究不是已24小時自動監控。

## 測試與交付狀態

新增test_forward_v09涵蓋初始化、二/三次非零部位恢復、同資料成交/平倉/funding冪等、缺棒/延遲/cursor修訂、版本改變、Artifact缺失/過期/不回退、partial entry、TP/stop保守順序、現金成本對帳、全部風控與日內鎖定、未知新聞、隔離、atomic中斷、併發與zip路徑安全。合成資料只用在明示測試，生產CLI不接受SYNTHETIC_TEST來源。

開發時曾抓到匯入語法、事件payload共享參照及tuple/list序列化差異，均於雲端凍結前修正。完整舊測試與新測試結果及實際雲端I/II/III證據會存forward_results/，不拿歷史10筆/3筆當新前瞻績效。


## 最終真實驗收與使用者政策

使用者已明確確認「維持嚴格資格門檻，只記錄拒絕候選，暫不建紙上部位」。現行凍結版已符合，沒有更動或重置版本。真實I/II/III均success，後兩次真正workflow_dispatch；root state revision 1→2→3、parent Artifact與journal前綴均驗證。第三輪財務指紋及trades/equity/positions檔案完全相同，損毀/缺失副本安全停止。詳細實際連結、scope與數字在forward_results/RESULTS.md，不以歷史績效冒充前瞻。

完整本地436項（含50項新測試）全通過；每次雲端均執行固定版50項必要測試。真實前瞻候選0、進場0、平倉0、cash10000、費用預算1000。實際兩次行情仍在相同已完成K線水位，只有新公開mark與觀測曲線點，未等待新完整K線或1–3日交易結果；非零部位只在明示合成測試中驗證。這是狀態與資料管線驗收，不是策略獲利驗收。

手動恢復及重跑指令已實際成功，首次註冊404限制已解除；不代表schedule已啟用。長期封存目的地、正式外部自動觸發及過期後人工恢復仍需使用者另外授權/操作。沒有取得交易Secret，沒有修改master/Render/正式推播或下單設定。
