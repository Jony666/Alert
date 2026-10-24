# 第五階段真實跨次雲端驗收

**政策：使用者已明確選擇維持嚴格資格門檻，只記錄拒絕候選，暫不建立B級紙上部位。既有凍結策略與帳本不變。**

|輪次|觸發|結果|前次狀態|revision|真實前瞻已平倉|研究cash|
|---|---|---|---|---:|---:|---:|
|[I 初始化](https://github.com/Jony666/Alert/actions/runs/40097744951)|研究分支註冊push|success|無，首次合法初始化|1|0|10000|
|[II 恢復](https://github.com/Jony666/Alert/actions/runs/40098344209)|workflow_dispatch|success|I / Artifact 12562075458|2|0|10000|
|[III 同資料與故障](https://github.com/Jony666/Alert/actions/runs/40098537490)|workflow_dispatch|success|II / Artifact 12562106009|3|0|10000|

第三輪Artifact 12562330593。全部檔案/狀態/事件鏈雜湊、genesis錨點、來源run ID/Artifact ID、parent state hash與revision均實際下載核對。I→II→III保留先前完整journal前綴；II/III trades.csv、equity.csv、positions.json逐位元組相同，financial hash相同；III的缺失/損毀副本安全拒絕且來源不變。

第一次workflow_dispatch的實際HTTP404是新workflow尚未註冊；用僅新workflow檔案的研究分支push註冊後，II/III已真正透過workflow_dispatch成功，並非只新增一個未執行的手動按鈕。沒有為此修改master。

本地完整436項測試通過，前4階段386項保留；三次雲端各執行凍結版50項快速必要測試。非零部位、部分進出、TP與funding/成本延續是明示合成測試；真實雲端帳本尚無非零部位，不能宣稱已觀察到真實前瞻平倉。

I/II各取得90組真實公開行情，195300根210日暖機，沒有將暖機歷史變成前瞻成交。兩次仍處於相同的已確認K線水位，II新增公開mark觀察與equity列（1→2），不補造歷史候選。III只重跑II已保存真實snapshot，不是第三次新行情取得。

策略版本 `0.9.5-6ecc69ea1107513b`；study `okx-v095-4e85ee42786c6564`；完整config hash `4e85ee42786c656414f4a038093fcb12ce2516bdb1f5e6cdbe6b3752b578c5fa`。
凍結source `e8863f819f29ef0cf2806ea06157aae7ac3a633e`；freeze UTC `2026-10-24T13:20:30.509400+00:00`；research start UTC `2026-10-24T13:24:55.950187+00:00`。

|策略|候選|准入通過|已進場|未平倉|已平倉|勝率/盈虧比/PF|
|---|---:|---:|---:|---:|---:|---|
|SUPPORT_RESISTANCE_REVERSAL|0|0|0|0|0|未驗證（無交易樣本）|
|VOLATILITY_CONTRACTION_BREAKOUT|0|0|0|0|0|未驗證（無交易樣本）|

目前權益10000、費用預算剩1000、實際費用/funding/模擬滑價均0、浮盈0、觀測回撤0%。零回撤/零成交不是有效策略的證據，也不是歷史50%/66.67%的前瞻重現。

現行策略沒有安裝可信60%/獨立樣本及官方全倉證據，當前新聞覆蓋是UNKNOWN而不是無事件。使用者已確認維持此嚴格准入，故後續有技術訊號時也只記拒絕理由，不會自動鬆綁為研究性部位。

## 待解決與人工操作

- 真實非零前瞻成交/退出與足夠獨立樣本尚未出現；凍結後只有分鐘級時間。不能把合成測試或過去行情當前瞻績效。
- 官方全倉帳戶模式/全部擔保品/歷史清算規則/實際費率、逐筆成交排隊、funding時點標記價及完整當前事件覆蓋仍未驗證。
- 沒有自動schedule或24小時常駐服務。可用已驗證的手動dispatch續跑；7日狀態過期即停止，不能拿重新initialize繞過。
- 真正自動定時需另行批准不修改master的外部觸發方案；建議使用者既有排程器每小時收盤後5–10分鐘呼叫dispatch，不能請求交易Secret或變更正式排程。
- Artifact實際保留90天、最早約2027-01-22過期，不是永久資料庫。需要使用者另行指定/授權永久儲存或定期完整匯出目的地；目前不新增帳戶或服務。
- 每次最新Artifact含完整累計交易/事件/曲線及210日市場快取。定期用gh run download下載整份bundle，連同freeze、anchor、run ID及manifest保存；最新合法state若遺失/過期，程式不回退或清零，人工恢復程序須依已驗證匯出另行審核。

完整新增/修改清單見FILES.txt；三個run_manifest、summary與GitHub job metadata以及cloud-continuity.json提供實際可核對證據。
