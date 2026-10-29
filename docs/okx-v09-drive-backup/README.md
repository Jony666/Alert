# v0.9 Google Drive 長期備份（尚未啟用）

這是疊在 PR #3 報表工具上的獨立開發功能，不變更策略、資金風控、正式前瞻排程、Render、推播或下單。`master` 與 PR #1／#2／#3 不合併、不推送。

指定目的地：`1OlXqvYUPTl7sK5Eds71X_3gcRUQv1G-q`。目前 `config.json` 的 `live_upload_enabled=false`；正式排程範本放在文件目錄、附名 `.disabled`，不會被 GitHub 註冊。唯一新增且會執行的 workflow 是本開發分支的驗證 CI；它沒有 Google 憑證、沒有 schedule，只使用模擬 Drive。

## 狀態與驗收界線

- 已實作：最新成功來源選取、完整 Artifact 驗證、報表、原始 ZIP、版本與 SHA-256 封裝、create-only 上傳、下載核對、原樣還原、獨立版本索引、失敗重試、重複備份去重、歷史還原抽查、到期風險提示及安全錯誤碼。
- 自動測試使用真實 GitHub 研究 Artifact 加上記憶體中的 Drive 協定模擬；模擬上傳不等於真正 Google Drive 驗收。
- **尚待使用者完成獨立 Google OAuth、資料夾存取與配額驗證，並批准單次正式上傳／下載還原測試。每日正式備份另外等待最後批准及部署審核。** 不會透過 ChatGPT 連接器代替 Actions 的 OAuth，也不會因失敗擴大 scope。

## 最小權限：個人 Google OAuth + 明確選取資料夾

僅使用 `https://www.googleapis.com/auth/drive.file`。知道資料夾 ID、分享網址、或 ChatGPT 已連接 Drive，都不代表新 OAuth App 有權限。個人 My Drive 不假設 Service Account 有可用儲存配額；本工具拒絕 Service Account 和未驗證配額，也不把共享雲端硬碟當成個人資料夾。

Google 於 2026 年的官方 Desktop Picker 流程可在 Desktop OAuth 中使用 `trigger_onepick=true` 與 PKCE，無須另外架設 JavaScript 網站或交付 Picker API key。本工具固定要求資料夾 MIME、單選、`file_ids` 指定 ID；回呼的 `picked_file_ids` 必須恰好等於指定 ID，並透過同一 App 的 `files.get` 核對可寫能力。若 Google 尚未向您的 App 開放此流程、選取失敗、`drive.file` 回傳 403／404、或無配額，**停止，不要求整個 Drive 權限**。

由使用者在自己的電腦操作，請勿把密碼、Token 或 JSON 內容貼到聊天、PR 或日誌：

1. 在自己的 Google Cloud 專案啟用 **Google Drive API**。設定 OAuth consent screen，僅加入 `drive.file`，建立 **Desktop app** OAuth client，下載 client JSON 至儲存庫外的私人目錄。使用與指定 My Drive 資料夾相同的個人帳號。
2. 注意：External App 的 **Testing** 模式通常使這類 refresh token 七天到期。長期排程前需依 Google 規範設定 Production publishing status；`drive.file` 是建議的非敏感權限，但仍需遵循帳號／組織政策與 Google 審核要求。工具不能保證 refresh token 永遠有效。
3. 取得本 PR 經審核程式與 Python 3.12／requests，在 repo 根目錄執行（路徑請自行替換）：

   ```bash
   PYTHONPATH=tools python -m okx_v09_drive_backup.authorize \
     --client-json "$HOME/.config/okx-v09/desktop-client.json" \
     --credentials-out "$HOME/.config/okx-v09/drive-authorized-user.json"
   ```

   helper 只在個人電腦執行，綁定 `127.0.0.1` 隨機埠，用 state 與 PKCE；不記錄 callback URL、code、Token。請在 Google 頁面**明確選取指定資料夾**並檢查只有 per-file 存取。成功後僅驗證資料夾讀取及 `canAddChildren`，**不會上傳**。輸出檔限儲存庫外、權限 `0600`、禁止覆蓋既有檔案或沿用 symlink。
4. 授權／資料夾驗證若失敗，保留 `drive.file`。安全替代方案是：維持離線備份並人工上傳，或由使用者另行批准 App 建立的新資料夾／支援的 Picker 流程；這次程式不會自動改目的地、建立資料夾或換廣泛 scope。
5. 在 GitHub 建立未啟用的保護環境 `okx-v09-drive-backup-approval`，限制可用分支與需要的審核者。由使用者在本機登入 `gh`，將檔案經 stdin 存入該環境 Secret：

   ```bash
   gh secret set OKX_V09_DRIVE_OAUTH --repo Jony666/Alert \
     --env okx-v09-drive-backup-approval \
     < "$HOME/.config/okx-v09/drive-authorized-user.json"
   ```

   不使用 `echo`、命令列 literal、`cat`、Actions output 或 Artifact 傳遞憑證。GitHub 的 OAuth 憑證與 ChatGPT OAuth 完全分開。保留私人本機檔案時請妥善保護；撤銷 App／Secret 需由使用者另外操作。
6. **此時仍未啟用。** 單次上傳前需使用者明確批准、審核固定程式 SHA、將專用設定啟用，以及受保護環境的 `OKX_V09_DRIVE_UPLOAD_APPROVAL` 設成 `I_APPROVE_ONE_DRIVE_BACKUP`。這個值只是操作閘門，不是密碼，也不能取代 GitHub 權限／使用者批准。檔案中的 config 與環境 approval 必須同時允許，否則會在讀 Secret 前停止。
7. 單次測試完成、實際下載 SHA 與恢復成功後，再另外批准每日 schedule 的預設分支部署。現在不能自行把範本移至 `.github/workflows`、修改 `master` 或調整 Render。未來若部署到受 Render 監控的分支，須重新依授權處理隔離，不能假設本 PR 已授權部署。

## 操作與備份內容

GET-only 預覽（不讀 Google Secret、不建立 Google client）：

```bash
PYTHONPATH=tools GH_TOKEN="${GH_TOKEN}" python -m okx_v09_drive_backup plan \
  --config docs/okx-v09-drive-backup/config.json \
  --model-root /path/to/pinned-frozen/okx-monitor/backtest \
  --result /private/new-directory/plan.json
```

未啟用設定下執行 `upload` 會直接失敗；請不要透過 shell 除錯列印環境變數。上傳由未來已批准 workflow 注入環境 Secret，本次不執行。

每個 ZIP 內保留：

- `backup/original-artifact.zip`：GitHub 下載的原始 bytes；完整 22 個前瞻 bundle 檔案不刪減。
- `backup/source-context.json`、`source-inventory.json`、`tool-version.json`、`backup-manifest.json`。
- `report/`：繁中 Markdown／HTML、`equity.svg`、逐筆交易與候選／資金等 CSV、`metrics.json`、`report-manifest.json`。
- `package-manifest.json`：所有 payload SHA-256、原始 ZIP SHA、Run／Artifact ID、revision、frozen model SHA、config hash、report／exporter 版本與程式 inventory 指紋。沒有憑證或可執行模型。

Drive 上另存每次 intent、已驗證 index、重複備份驗證 audit 與 restore-audit JSON。**沒有會被覆寫的 LATEST 檔案**。索引是所有 `kind=verified-index` 的不可變 JSON 集合。每筆索引都含來源 Run ID、Artifact ID、revision、原始 ZIP SHA、備份 ZIP SHA／大小／file ID、local manifest seal 與驗證狀態。檔名不是身分，所有查找依固定 parent 與 App properties。

## 最新性、恢復及重試

1. GET 正式 workflow 在 master 的成功紀錄，依完成更新時間選最新成功執行（不依 run_number 猜測排隊順序）；拒絕待另行審核的 rerun。必須恰好找到該 run 的正式狀態 Artifact。
2. 對照 repo Artifact metadata，若出現較新的已發布研究狀態卻不屬於最新成功 run，停止。最新檔案遺失、到期、重複、摘要不符都停止，**不退回舊 Artifact**。ZIP 只下載選中的最新成功來源；測試固定 revision 19／20 是回歸 fixtures，不是正式備份選取邏輯。
3. 先沿用 PR #3 全部原始驗證，並驗證凍結程式 SHA。對照已備份最大 revision，禁止倒退／同 revision 分叉；跨日版本須保有 journal、closed trades prefix 與遞增 watermark。每日累計備份無法代替缺失中間每小時原始檔的完整 parent chain 證明，故不宣稱逐輪所有 parent 都已驗證。
4. 以來源身分＋工具版本＋程式指紋形成 backup key。每個新來源或經審核的工具版本建立獨立 ZIP，不替換舊檔。同版本同來源重複執行會完整重新下載驗證，留下 audit，不建立第二份相同 ZIP。
5. 預先產生 Drive file IDs，create-only resumable upload。對 429、rate-limit 403、可恢復網路錯誤及 5xx 有上限退避；不重試提升權限。對不確定 PUT 結果先查 session offset，依 308 Range 續傳；session 到期後以相同 file ID 新建 session，409 只接受既有 bytes 完全一致。
6. **提交 verified-index 前**，完整下載 ZIP 核對大小／本機 SHA-256；Google 有提供 `sha256Checksum` 時另外核對，但不以該欄位取代下載。解開驗證全部內層 SHA、原始研究狀態及報表，呼叫原備份還原工具，在新的暫存目錄比對原始 ZIP 與 22 個 bundle 檔案 bytes。此還原不會寫回正式帳本。
7. 網路故障後完成 ZIP 但未提交 index，可依 immutable intent 找回、完整重新驗證並補建 index；未完成的 intent 保留，新嘗試使用新 ID，不刪除／覆寫。多個完整 intent、索引損壞或 fork 需人工審查，不隱藏失敗。
8. 寫 intent 前與提交 index 前重新檢查來源；若期間前瞻狀態更新，停止且保留未標成 verified 的證據，後續重試從新來源開始。索引的「最新」只代表最後核對時間點，不保證上傳期間世界不再更新。正式單一 repo concurrency 必須防止同時 writer；另一 repo／人工 writer 不在鎖範圍內，禁止同時使用此 App 寫入。

## 定期還原與 90 天到期

每個新 ZIP 已有下載＋完整離線還原。每次成功執行另檢查歷史版本：挑最久未抽查的一份，若未查過或距上次七天，下載並還原一次，另存 immutable restore-audit；每日最多一份，以輪替方式涵蓋版本。**不是保證所有歷史版本每七天重查一次**，版本多時應另行審核擴大抽查頻率；舊檔不會被清理。

每日 UTC `0 0 * * *`（台灣 08:00）是待批准的設計。GitHub schedule 可能延遲或因 inactive repo 暫停，不能當成準時保證。要在 GitHub Actions 頁面確認成功與綠色索引；失敗用非零 exit、FAILED 安全碼與 Run failure 明確呈現，不發送 Email／Telegram／LINE。GitHub 帳號本身的通知偏好由使用者控制，本工具不設定推播。

來源 index 記錄到期時間及剩餘天數，低於 30 天顯示 warning；掃描較舊每小時 Artifact metadata，列出即將到期數與最早期限。**每日只備份最新成功的累計帳本，不等於保存所有每小時原始 ZIP**；累計交易／事件歷史保留，但舊 raw snapshots 若未另行授權批次匯出仍會到期。本 PR 不擅自歷史補傳。真正每日備份尚未開啟，所以現有 Artifact 的 90 天風險仍未消除。

若最新來源已到期，不可 fallback、重建帳本或偽造歷史；應停止研究備份並人工處理。若 Google Token 被撤銷／到期，重新在本機最小權限授權及更新 Secret；不得把 refresh error body、resumable URL、code 或 token 寫入日誌。

## 官方依據與限制

- [Drive OAuth scopes / drive.file](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Desktop / mobile Picker，trigger_onepick 與資料夾選取](https://developers.google.com/workspace/drive/picker/guides/desktop-mobile-picker)
- [Desktop OAuth / loopback / PKCE](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Refresh token 與 Testing 模式期限](https://developers.google.com/identity/protocols/oauth2)
- [Resumable uploads 與 pre-generated ID](https://developers.google.com/workspace/drive/api/guides/manage-uploads)
- [files.generateIds](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/generateIds)
- [files SHA-256 metadata](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)
- [about.get / storageQuota](https://developers.google.com/workspace/drive/api/reference/rest/v3/about/get)

查閱日期：2026-10-29。文件支援不等於本指定帳號／資料夾已授權，真實 OAuth、可寫權限與實際配額仍待使用者驗收。SHA-256 可發現意外毀損，不能防止同一憑證擁有者同時惡意重寫檔案及摘要；必要時可另外離線保存 index seals。這次沒有部署外部服務、備份簽章金鑰、監控服務或任何交易憑證。
