# 全倉模型的輸入、計算與拒絕邊界

`margin.cross_account(snapshot, positions, tiers, at)` 不連接帳戶、不讀 API 金鑰。它檢查數值、時間和級距後，回傳研究情境；**任何結果的 allow_recommendation 都是 False**。使用者自述總帳戶約USD1000不是可代入的完整全倉現金，本模型沒有拿這個數字擴大專案本金。

輸入 snapshot 必須明確提供 `cash_usdt`、`liabilities_usdt`、`other_collateral_haircut_usdt`（已折價後可用擔保品）、`reserved_fees_usdt`、`funding_due_usdt`、`time`、`reconciled`。時間要有時區，預設不得超過300秒或來自未來，數值必須有限且非負。`reconciled` 必須代表所有帳戶資產、負債、其他未平倉和保留費用都已對齊，不能只看到本專案幾筆就自稱全帳戶核實。

每個 position 提供 `symbol, units, entry, mark, mark_time, leverage, side`。tiers 為依 symbol 索引的 `effective_from, valid_until, tiers`，每段明確提供 `max_units, mmr, imr, deduction_usdt`。units 是供給級距對應的基礎幣單位，非未換算的合約張數；必須先按同時點ctVal換算。不擅自猜測官方MMR、級距或扣減額，也不把當前級距套入歷史當作當時可知。

每個壓力情境依每筆方向給不利標記價格：多單下跌、空單上漲，同時計入共享權益：

- 權益 = 現金＋已折價其他擔保品＋全部未實現損益－負債－保留費用－待付funding。
- 初始保證金需求 = Σ名義價值／槓桿。
- 維持需求 = Σmax(名義價值×供給MMR－供給deduction, 0)＋名義價值×0.001費用預算。
- ratio = 權益／維持需求；權益≤維持需求標記模型缺口，不冒稱已在OKX強平。

0.001是明示壓力費用預算，並非聲稱官方清算費。輸出0/1/3/5/10/20%不利跳空；二分法尋找共同不利情境下的維持缺口界線。這是對不同資產共同不利的壓力，不是單一幣種官方強平價；多空同時不利亦不是相同價格因子的對沖模型。維持缺口界線可驗算，但不能用來向實盤建議100倍。

尚未驗證：OKX帳戶模式（單幣、多幣、組合保證金）、擔保品動態折扣、歷史級距、官方標記/指數價格、實際清算/減倉/ADL、平台故障與真實委託簿。缺任何必要資料即NOT VERIFIED；即使研究輸入完整，仍是MODEL_ONLY_NOT_OFFICIAL_OKX，必須拒絕交易建議。所有現有策略的資格判斷與候選報告都保留這項阻擋。
