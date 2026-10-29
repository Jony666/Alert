# 開發版安全審查與驗收方式

## 修改範圍

本分支以 PR #3 的 `21e6ca6a7dcbc00fd30791c13dcf5fb9f0646925` 為基礎，PR 目標是 `research/okx-v09-phase7-reporting`，不是 `master`。不變更原有報表／備份工具；新增的 Drive transport 重用其驗證與還原 API。

- `tools/okx_v09_drive_backup/`：獨立 policy、Desktop OAuth、GitHub 來源、Drive transport、封裝、不可變索引與 CLI。
- `tests/drive_backup/`：記憶體 Drive 協定模擬與對抗測試。
- `.github/scripts/drive_backup_acceptance.py`：實際選取最新 GitHub 來源，但只在模擬 Drive 上驗證。
- `.github/workflows/okx-v09-drive-backup-validation.yml`：僅開發分支 push 觸發、只讀 GitHub 權限，無 Google Secrets。
- `docs/okx-v09-drive-backup/`：停用設定、未啟用的每日 workflow 設計、使用者授權與驗收指引。

正式 forward workflow、兩套策略、NT$10,000 本金／NT$1,000 費用準備金及全部門檻均無修改。工具沒有 Render API、通知 transport 或 OKX 下單 client。

## 網路與憑證邊界

| 功能 | 目的地／方法 | 目前是否實際執行 |
|---|---|---|
| 公開官方文件核對 | developers.google.com / GET | 是 |
| GitHub 來源與 CI | api.github.com / GET、Artifact 下載轉址 | 是，開發環境部分 Artifact 主機被代理阻擋，另由 GitHub CI 驗證 |
| OAuth helper | accounts.google.com + oauth2.googleapis.com；code exchange／refresh | 否，只有 mock 測試 |
| Drive preflight／讀取 | www.googleapis.com/drive/v3 / GET | 否，只有 mock 測試 |
| Drive 上傳 | 固定 API 建立 resumable session / POST，受信任 Google upload URL / PUT | 否，只有 mock 測試 |
| Drive update／delete | PATCH／DELETE | 禁止 |
| Render／推播／下單 | 無實作 | 否 |

OAuth 必須是使用者自己的 Desktop App，scope 精確等於 `drive.file`，Picker 恰好選定目標，並核對同 App 的資料夾 `canAddChildren`。不使用 ChatGPT Drive connector，不要求廣泛 Drive scope，不接受 Service Account。Secret 必須由使用者經 stdin 存入 GitHub 環境，現在並未設定或讀取它。

## 驗收分層

1. 保留並重跑原有 436 項凍結模型測試、28 項規格審核測試、17 項狀態交接測試、52 項 PR #3 報表／備份測試，共 533 項。
2. 新測試覆蓋 policy、PKCE／state／folder 選取、Secret 隔離、scope 擴大拒絕、配額未知／不足、429／403 rate limit、不可恢復權限錯誤、chunk offset、過期 session、409、重試上限、完整下載 SHA、重複去重、來源競態、revision 倒退、索引中斷、歷史恢復及停用 schedule。
3. CI 使用 GitHub 最新正式成功 Artifact 進行完整驗證，然後阻擋所有 requests 網路，使用 MemoryDrive 實作上傳→下載→restore→重複執行。此結果明確標記 `SIMULATED_DRIVE_ACCEPTANCE_PASSED`、`live_google_upload_or_restore_passed=false`。
4. 使用者尚未授權的真實單次測試：OAuth／Picker、指定資料夾存取、個人配額、真正上傳、真正下載與完整恢復。必須另行批准，不能由第 3 項替代。
5. 每日正式排程啟用與首輪、跨輪次、七天歷史還原抽查皆為後續部署驗收，這次不會提前宣稱通過。

## 後續單次正式測試清單（未執行）

在使用者批准且完成 README 的同 App OAuth、環境 Secret、經審核啟用設定與固定 code SHA 後：

1. 執行 `plan`；記錄來源 Run ID／Artifact ID／revision／原始 ZIP SHA 和到期日期，確認沒有 Google 存取。
2. 以保護環境明確批准單次 `upload`。檢查完整 ZIP、繁中報表、交易與資金曲線、版本清單、intent／verified-index 都保存於指定資料夾。
3. 確認下載 SHA、restore 全部 22 個 bundle bytes 與原始 ZIP 相同，索引含正確 Run／Artifact／revision，狀態為 `UPLOADED_DOWNLOADED_SHA256_AND_RESTORE_VERIFIED`。
4. 相同來源重跑：`ALREADY_VERIFIED`，不增加第二份 ZIP，不修改第一份，新增驗證 audit。
5. 在隔離測試目的地另行批准故障演練；正式資料夾不得刻意損毀檔案。CI 內已以模擬環境測試中斷與恢復。
6. 驗收失敗時保留所有檔案、非零 exit 並停止，不擴大 scope、不回退舊帳本、不刪除 intent。
7. 全部通過後才另外批准每日部署。設定為 UTC `0 0 * * *`；GitHub schedule 可能延遲。這次沒有授權修改預設分支。

## 已知限制

- 指定資料夾的真實 OAuth 存取、實際 Google quota／速率限制／外部政策尚未驗證；官方文件可用不等於個人帳號實測通過。
- Token 可能到期或撤銷，Testing 模式通常七天期限；必須由使用者重新授權，沒有自動擴權或輸出 refresh error body。
- 目前只支援個人 My Drive；配額欄位未知則保守拒絕。ZIP 上限 64 MiB，超出需獨立審核分片方案，不能刪減原始資料。
- 只讀取同 OAuth App 可見的索引。**必須保留相同 Google Cloud 專案／OAuth client**；更換 App 前須另行審核既有備份授權與索引移交，不可把新 App 的空目錄視為沒有備份。
- 索引不可變、沒有覆寫 LATEST；沒有分散式鎖。正式範本的 concurrency 只能鎖同 GitHub repo，不允許外部或其他 repo 同時 writer。
- Drive metadata／索引的總頁數上限 100 頁，每頁 100；超出則明確停止，不截斷後宣稱完整。
- SHA-256 不是防惡意竄改簽章；同一授權者同時改 payload 與索引無法僅靠摘要檢出。可另存離線 seals，但未新增外部信任服務。
- 每日只保存最新成功累計帳本；逐筆交易與 journal 累計歷史會保留，但未備份的每小時原始 Artifact 仍會到期。未授權歷史批次外傳。
- 歷史版本每次最多抽查一份，依最久未檢查排序；版本累積時不保證每份七天重驗一次。每次新 ZIP 仍必須即時完整下載與恢復。
- 跨日缺少所有中間 raw Artifact 的逐輪 parent chain，僅驗證保留歷史 prefix／watermark 及各版本本身；不宣稱此替代完整逐小時鏈。
- v0.9 策略仍依嚴格資格門檻只記錄拒絕候選；備份不改善勝率、不創造交易，也不解除停機。
