# v0.9 首輪驗收與風險限制

此為 commit 87c0384 的歷史驗收紀錄。第二次開發狀態、331項測試與成功 Actions 見 ROUND2.md 及 round2_results/，新工作流程已支援嚴格限定分支與路徑的 push。

附件的開發規格已完整閱讀；真正授權以使用者本次訊息為準：只在獨立分支實作與開 PR，不合併、不改預設分支、不碰 v0.7/v0.8、不碰 Render、不推播、不實盤。

| 規格 | 實作／測試證據 | 結果與限制 |
|---|---|---|
| 已收盤資料、缺棒、重複、時區 | data.py；DataTests | 通過；歷史下市全集沒有取得，不冒稱覆蓋 |
| 三策略、多空、因果 | strategies.py；StrategyRulesV09Tests、prefix invariance | 通過；獨立固定規則，不以結果反調參 |
| 下一棒入場、分區隔離 | research.py；IntegratedV09Tests、embargo test | 通過；固定參數滾動兩區塊，非重訓模型 |
| 訓練／驗證／保留集 | summary.json splits、126 情境 | 完成；pristine 歷史不可外部認證，前瞻尚無證據 |
| 勝率 60% 可靠性 | evidence／Wilson 95% 下界、≥100、前瞻與 pristine flags | 門檻通過測試；本次無任何真正合格訊號 |
| 含全部成本淨 RR≥3 | exits.py、weighted RR／double costs tests | 通過；計畫與實際淨 R 分開 |
| 動態分批與追蹤 | structure_plan、bar_exit、report.py | 通過；先止損，追蹤下一棒生效、7 日退出；TP 不保證成交 |
| 1000／2000、2／3 部位 | RiskTests、共享 ALL 整合測試 | 通過；多策略共享同一紙上風控，不是分策略各自偷放總額 |
| 階梯與核實本金 | single_cap、unrealized_does_not_raise_cap | 通過；實際提高需人工核實，不用回測提高真實限額 |
| 20% 回撤與費用 ≤500 | State 保存載入、持續停機 tests | 通過；無隔日自動恢復、無自動解鎖 |
| 台北同日連虧、第三特例、第四禁止 | RiskTests、ChronologyBoundaryTests | 通過；第三不同策略只有研究標籤，不自動允許下單 |
| 全倉與槓桿 | cross_stress、no_100x、exchange max leverage | 風控拒絕路徑通過；官方全倉模型未完成，5/10/20 亦僅紙上假設 |
| 流動性、單位、tick | data/catalog、size、quantize_plan | 通過；沒有實際委託簿、完全成交保證 |
| 高相關共同跳空 | ALL scenario max_joint_20pct_gap_loss_twd | 有共同跳空壓力；未完成實證相關性／穩定性排序 |
| 新聞時間、來源、不捏造加減分 | events.py、EvidenceEventTests、--events | 通過；未建完整歷史新聞庫與事件勝率模型 |
| 候選固定報告 | EXAMPLE_REPORT.md、candidate_reports.jsonl.gz | 完成；歷史紙上範例，無實際出場捏造，無當下合格建議 |
| 手動成交與分帳 | LedgerReportTests、MoreBoundaryV09Tests | 通過；部分進出、成本、出入金、補對帳、保存；未串接聊天／真實 API |
| 新舊回歸測試 | results/unit-tests.txt | 完整 Python discovery 320 項全通過，其中 v0.9 93 項 |
| 原系統與部署邊界 | git diff 僅新增允許清單、SafetyTests | v0.7/v0.8/Render/原監控/推播未修改；未調用交易或部署 API |
| GitHub Actions | 新 okx-swing-v09.yml，手動、contents:read | 檔案完成；實際雲端執行結果以 PR 與交付回覆為準，不能以本地通過充當雲端通過 |

紙上回測不冒充真實人工帳戶；原始 API 資料與合約快照皆封存。匯率 32 TWD/USDT 為固定假設，未聲稱為實際匯率。全倉共享資產與資金池無物理隔離，止損金額是計畫預算而非封頂承諾。超過預算及 20% 的歷史虧損仍如實呈現，停機僅禁止新倉，不倒填一個理想止損成交。

尚未完成的研究資料與模型請見 README「尚未實作／尚未證明」，不因單元測試全通過就宣稱需求中所有實盤可信度已被證明。
