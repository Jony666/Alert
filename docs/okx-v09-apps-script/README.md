# 第七階段延伸：免網域 Apps Script 備份驗證版（未部署）

評估日期：**2026-10-30，Asia/Taipei**。本分支以 PR #4 `9fa92da23d9de9616db22313bb03452873fbf866` 為基礎，PR base 為 `research/okx-v09-phase7-drive-backup`。原有 610 項測試、Python 報表／完整驗證／還原工具完全不改。本次只新增獨立程式與模擬測試，不修改 master、正式研究排程、交易策略、風控、Render、推播或下單。沒有 Apps Script API 部署、clasp push、Google Token 或真實 Drive 上傳。

## 可行性結論與明確界線

**官方文件支持私人個人帳號、免自有網域、Picker + `drive.file` 的技術路徑；只適合目前的小型封包，尚未完成帳號／Google 執行環境驗收。不是已可無限期無人值守的安全證明。**

1. 個人帳號可以建立 installable time-driven trigger，執行身分是建立者。私人試算表綁定 script 與 HTML dialog 可以使用 Google 託管介面，不需購買網域、不需部署公開 web app 或 Marketplace App。
2. Google OAuth 政策對「只供自己／少於 100 個本人認識的人使用」與公開 production app 有區別；驗證品牌、公開應用程式與 Cloud Console 的 publishing status 也不是同一件事。**不能因介面出現網域欄位，就斷言必須購買網域；也不能保證每個帳號均可略過 Google 的政策／警告。** 不虛構網域、不假裝擁有 google.com。
3. Apps Script 官方 Picker 範例的 manifest 明列 `drive.file`；需要 **standard Cloud project**，Picker App ID 必須為同一 project number，並傳同一 Script OAuth token。可明確選取既有指定資料夾，再 GET 驗證 ID、folder MIME、canAddChildren 及個人配額。只有資料夾 ID、Desktop client 已存在、或 ChatGPT 已連 Drive，都不能當成 Script 已授權。
4. 不使用 `DriveApp`。官方 `DriveApp.createFile` 要求完整 `drive`，即使程式只寫一個 folder 也不是 OAuth 最小權限。本版使用 Drive v3 REST；Advanced Drive Service 是可選的同 API 封裝，**不是擴權或略過 Picker 的方法**。
5. Apps Script 是 V8 JavaScript，不能直接執行 PR #3/#4 的 Python 完整金融／帳本驗證器。因此採**混合架構**：獨立 GitHub 準備工作流程沿用原 Python，先完整驗證、報表、封裝、離線還原；Apps Script 只接受固定 producer workflow ID／head SHA／工具指紋的成功 Artifact，核對所有封裝 SHA 並搬運。這不是把 SHA 取代金融語意驗證。
6. 這個獨立 producer 尚未部署；文件中只有 `.disabled` workflow 設計。未來若不批准獨立 producer，就不能啟用本 Apps Script consumer。**不得把原始研究 Artifact 直接搬走後稱為已有繁中報表及完整 Python 驗證。**
7. `enabled=false`、producer pins 為 `REVIEW_REQUIRED`／0，沒有 Google 或 GitHub Secret。這些設定會在任何外部讀取前拒絕備份。Picker setup 是分開的手動授權步驟，沒有上傳或安裝 trigger。

## 先檢視的官方依據

| 官方資料 | 支持／限制 |
|---|---|
| [Apps Script Picker](https://developers.google.com/apps-script/guides/dialogs#file-open_dialogs) | `drive.file`、standard project、相同 project number、OAuth token、API key referrer 限制 |
| [DriveApp](https://developers.google.com/apps-script/reference/drive/drive-app) | `createFile` 的 OAuth scope 是完整 `drive`，本版不用 |
| [OAuth policies](https://developers.google.com/identity/protocols/oauth2/policies) | production 定義區分 personal use；個人使用不等於公開 app 的品牌驗證要求 |
| [Verification Help](https://support.google.com/cloud/answer/9110914?hl=en) | 只用 non-sensitive scope 不強制 app verification；品牌顯示是另一種 verification |
| [OAuth refresh token expiry](https://developers.google.com/identity/protocols/oauth2) | External + Testing refresh token 七天期限，drive.file 不屬基本 profile 豁免 |
| [Installable triggers](https://developers.google.com/apps-script/guides/triggers/installable) | 個人建立者身分、standalone/bound script、觸發時間有隨機性 |
| [ClockTriggerBuilder](https://developers.google.com/apps-script/reference/script/clock-trigger-builder) | `atHour(8).nearMinute(0).everyDays(1).inTimezone('Asia/Taipei')`，nearMinute 正負 15 分鐘，非 SLA |
| [Apps Script quotas](https://developers.google.com/apps-script/guides/services/quotas) | 每次六分鐘、UrlFetch 回應／POST 50 MB、個人 trigger runtime 90 分鐘／日、UrlFetch 20,000／日、Properties 每值 9 KB／總量 500 KB；額度可能改變 |
| [V8 limitations](https://developers.google.com/apps-script/guides/v8-runtime) | 無 Node、原生 fetch、URL、WebCrypto、streams；production .gs 使用 Apps Script 原生 API |
| [Properties](https://developers.google.com/apps-script/guides/properties) | Script Properties 由 script 使用者共用，不是隔離的秘密保管庫 |
| [GitHub Artifact API](https://docs.github.com/en/rest/actions/artifacts#download-an-artifact) | Actions read；下載 302 短效 URL 一分鐘、410 過期、Artifact digest |

來源 URL、查閱時間、擷取文字 SHA 與精確重點另見 `official-evidence.json`。雜湊是查閱追溯，不保證官方將來不修改頁面。沒有將網站內容當成使用者命令執行。

## 隔離架構與完整備份

`正式研究 Artifact (只讀)` → `另行批准的固定 SHA Python producer` → `export.json + package.zip 的 GitHub Artifact` → `私人 Apps Script + Picker` → `指定 Drive folder`。

- Python producer `.github/scripts/apps_script_export.py` 直接呼叫 PR #4 的 `read_latest`、`prepare_package`、`verify_package` 與 Model，包含原始 ZIP、22 個原始 bundle 檔案、報表／交易／持倉／資金曲線、固定策略與版本、所有 SHA、source-context。它只寫本機 export 目錄，沒有 Google client。
- consumer 核對正式來源與 producer 的最新成功紀錄。來源與 export 不是同一個 Artifact，但 envelope 必須精確指向**當下最新成功正式來源**；若 GitHub producer 延遲而仍封裝舊來源，consumer 停止，不接受 fallback。來源超過 36 小時也拒絕並留下錯誤。
- GitHub 先以 API metadata SHA 驗證完整 export ZIP，再檢查兩個 export 檔案、package manifest、report manifest、local backup manifest seal、原始 ZIP SHA 及原始 22 檔 inventory。原始 raw ZIP 沒有重新壓製或改寫。
- 所有 Drive 檔案採預先產生 ID + create-only resumable upload。沒有 PATCH／DELETE。immutable intent → 完整 ZIP → 下載核對／展開驗證 → immutable verified-index → immutable audit。重跑同 key 完整驗證舊檔，回傳 ALREADY_VERIFIED，不再建立相同 ZIP。
- 不同 producer run 可能有不同驗證時間與外層 ZIP；版本 key 依原始來源身分和 PR #4 工具指紋決定。重複來源保留第一份已驗證封包及其原 producer metadata，不將舊檔換成新 timestamp。
- ZIP 中央目錄在 `Utilities.unzip` 前檢查：路徑、重複、加密、ZIP64、壓縮膨脹比及解壓總量。SHA 全部以 bytes 計算；Apps Script 不重新計算 Python 的浮點 JSON state hash，而是驗證 Python 產出的 raw bytes、seals 與固定 producer provenance。
- 對已備份最高 revision 檢查倒退、同 revision 分叉、journal／closed prefix、watermark。每日累計版本不能補足未保存的每小時 parent 原始檔鏈，限制保留不隱藏。
- `verifyRestore()` 下載已保存的完整封包、解開並逐檔核對原始 22 檔，留下 RESTORE_VERIFIED_OFFLINE_NO_LEDGER_IMPORT；每七天輪替抽查一份。**不會匯入或啟用正式研究帳本**。實際匯出到本機時可用 PR #4 的 `verify_package`／`restore_backup`，必須另選新目錄。

## 真實資料大小、時間與成本

本次基準：正式 Run 40851188958、Artifact 12886458799、revision 20。原 ZIP **5,472,179 bytes**，解開 **16,190,901 bytes／22 檔**；PR #4 完整 package 約 **5.51 MB**。這是實際檔案測量，不是虛構交易資料；當下最新來源仍需每次重新選取，不能把固定 fixture 當成正式最新。

consumer 保守限制：封包／UrlFetch 預先宣告 12 MiB、每層解壓總量 40 MiB、每次 240 秒預算並在網路／提交前預留時間。原始 source JSON、兩層封包及 JS byte arrays 同時存在時記憶體可能放大；**Node mock 測試不等於 Apps Script 的記憶體／六分鐘實測**。Google 未提供此程式的可用 heap 保證；必須在 Google 端做只讀 probe，超出或耗時不足即停止，不產生 verified-index。

單一 UrlFetch 無法由本程式強制中止；若 Google 在平台六分鐘硬停，Script lock 由平台釋放，既有 intent／有效版本不刪除，後續重新驗證。超過上限時不可刪欄位、只傳部分資料或改用 Base64 假裝成功；優先改用 PR #4 Python 備份，或另行審核預封裝分片＋全量 manifest／跨輪次狀態機，這次不自動切換。

以目前 5.51 MB 靜態大小一天一份約 2 GB／年，**不是未來成長預測或免費保證**。累計歷史會變大，Google 免費空間也與其他服務共用；每次檢查個人 quota，未知／不足拒絕。Apps Script 本身有免費額度，GitHub producer 會消耗帳號 Actions 分鐘／Artifact storage；請檢查現有方案，未承諾零費用、不建立付費服務。

## GitHub 授權安全

由使用者自己建立 **fine-grained PAT**：resource owner `Jony666` → Only select repositories → `Alert` → Repository permissions **Actions: Read-only**，Metadata read 是 GitHub 自帶。無需 Contents write、Actions write、workflow、repo classic scope 或任何 OKX Key。

- PAT 只由使用者在私人 Apps Script 的 Project Settings → Script Properties 填入 `OKX_V09_GITHUB_ACTIONS_READ_TOKEN`；不要貼聊天、原始碼、PR、log 或 Artifact。不要把 token 放在 URL query。
- **任何可編輯 script／綁定試算表的人都可能修改程式，取得 token 或藉建立者 trigger 外傳。** User Properties 也不能抵禦惡意專案編輯者。只允許自己為 owner/editor，不公開分享、不使用公開 web app，不把 Script Properties 當成 Secret Manager 等級隔離。
- GitHub 401 回報 TOKEN_EXPIRED_OR_REVOKED_REAUTHORIZE；403 權限／rate-limit 安全停止；429／5xx／網路中斷有限重試，不自動增加權限。使用有限效期 PAT，提前自行輪替；程式不會顯示舊 token。
- 302 Location 只在記憶體中使用；第二次對 GitHub storage host 的請求**不帶 GitHub Authorization**、不自動 follow redirects。僅接受已知 HTTPS Azure Actions artifact host，未知 host／過長 URL／過期短鏈 fail closed；不記錄 signed URL。GitHub 更換下載 host 時須審核，不擴大 allowlist 來碰運氣。
- Google OAuth token 由 `ScriptApp.getOAuthToken()` 即時取得，只作 Drive header 或官方 Picker 的私有頁面記憶體使用；不存 Google refresh token 到 Properties、GitHub、log 或 Artifact，不使用 ChatGPT 的連線。

## 使用者操作（現在不需交付任何憑證）

**請先審核本 PR，再選擇下方架構。以下都未自動執行。**

1. 若選 Apps Script：自己建立一份不分享的空白 Google 試算表，Extensions → Apps Script。未核准前不要貼入後立即執行備份／安裝 trigger。
2. 將 source 檔加入私人綁定專案，顯示並核對 `appsscript.json` 的四項 scope：`drive.file`（檔案）、`script.external_request`（GitHub／Drive HTTP）、`script.container.ui`（私人 Picker）、`script.scriptapp`（安裝時鐘觸發器）。**不是僅有一項 scope，也沒有完整 drive。** 若 Google 顯示更廣泛 Drive 存取，停止。
3. 綁定 standard Cloud project `OKX-v09-Drive-Backup` 的 project number（不是 Desktop client ID）。啟用 Drive API、Picker API，建立僅供 Picker 的 API key，依官方文件限定 API 與 Google 託管介面 referrer。填 Config 的 projectNumber／pickerApiKey；API key 不是 Google 使用者 OAuth Token。
4. 先確認專案 publishing status／personal-use 政策。External Testing 的 refresh token 七天限制不能忽略，也沒有證據顯示 Apps Script 一律豁免。需要長期授權時由使用者自行確認 Google 的個人使用設定；若政策或 UI 要求無法接受的條件，停在此處，選其他架構，不自動擴權。八天以上持續執行驗收仍須實測，不能只看首日綠燈。
5. 手動執行 `showFolderPicker()`，使用自己的 Google 帳號明確選取指定 ID。程式 GET 核對可寫能力、個人 quota 在備份前再查。若 403／404／選錯 folder／無 quota，停止，不換 drive scope、不換 Service Account。
6. 自己設定上述 GitHub PAT（僅 Alert／Actions read）。不要把內容傳給 AI。檢查指定資料夾是否已有 PR #4 或其他 App 的備份，決定保留並記錄索引移交／namespace；不同 OAuth App 的可見檔案不一定相同，不能把空 list 當成沒有歷史。啟用前須完成這項人工移交審核，才可將 `handoffReviewed` 從 false 改成 true；程式預設會拒絕未審核的 namespace。
7. **另外批准**獨立 producer workflow 部署，鎖定已審核 export code SHA、workflow ID、工具 fingerprint，填 Config pins；不合併整個 PR，不改正式研究排程。現在只有 `.disabled` 設計，不能直接複製後當成已批准。若涉及 master／Render 監控，必須另行處理授權隔離。
8. 單次真實試驗需另行批准：設定 `enabled=true` 及 Script Property `OKX_V09_UPLOAD_APPROVAL=I_APPROVE_ONE_APPS_SCRIPT_BACKUP`，手動 `backupOnce()`。這個字串只是操作閘門，不是密碼。檢查 intent、ZIP、verified-index、audit、Run／Artifact／revision 與下載 SHA；再執行 `verifyRestore()`。失敗保留所有檔案，不清空 ledger 或 index。
9. 驗收通過後才另外批准每日執行，設定 `OKX_V09_DAILY_APPROVAL=I_APPROVE_DAILY_APPS_SCRIPT_BACKUP`，手動 `installDailyTrigger()`。程式防止重複 trigger，不刪除其他 trigger。不應同時啟用 PR #4 與本方案兩個備份 writer。

## 排程、失敗與長期監控

- producer 設計為台灣 07:15（UTC 前一日 23:15），consumer 約 08:00；兩者均可能延遲或未執行。Apps Script 官方 nearMinute 正負 15 分鐘不是送達 SLA，不能宣稱 08:00:00 保證啟動。
- GitHub producer 延遲／漏跑且最新 export 不對應最新正式來源時，拒絕備份；Actions read PAT 不可 dispatch producer，故不升成 write。使用者可在 GitHub UI 手動重試已批准 producer，或等待後續成功。
- 每次成功／去重／還原在 Drive 新建 immutable audit；失敗輸出經清理的安全 code 至 Apps Script Execution log 與 LAST_FAILURE，不輸出原 exception、Token、HTTP body 或 URL。網路完全不可用時無法保證寫入 Drive error 檔，但不會回報成功。
- `checkBackupStatus()` 顯示最近真正備份 receipt／失敗；超過 36 小時顯示 BACKUP_OVERDUE。還原 audit 不得刷新「備份成功」時間來掩蓋漏跑。**若整個 trigger 沒啟動，不會有一個正在執行的程式主動告警**；目前沒有獨立 watchdog／Email／Telegram／LINE。Google 平台自帶 trigger failure 通知由使用者帳號設定控制。
- 本 script 的 `LockService.getScriptLock()` 防同專案重疊，不能鎖別的 Apps Script project、PR #4 Python 或其他 repo。只允許一個已批准 writer，不把這個鎖說成分散式鎖。
- source／export 到期日期記入索引及 receipt，剩 30 天警示。每日只保存最新累計帳本，不保證所有 hourly raw snapshots 均已保存；未啟用前 90 天風險未消除，沒有擅自歷史外傳或刪除到期前資料。
- SHA 不是惡意竄改簽章；同一 credential owner 同時竄改資料與 index 不能僅憑摘要防止。須保護 GitHub／Script 編輯權限，必要時另保存離線 seals。

## 替代方案（至少兩種，交由使用者選擇）

| 方案 | 實際權限／操作 | 網域、費用與維護 |
|---|---|---|
| **A：沿用 PR #4，個人 Desktop OAuth + Picker + Python Actions** | Google drive.file；憑證在 GitHub 環境 Secret；保留原完整 Python 還原；使用者處理 personal-use／publishing status，無須另加 Apps Script PAT | 不因 Desktop OAuth 本身要求買網域。已有 610 測試，沒有 Apps Script 六分鐘／50 MB 限制，平台較少。Google／Actions 額度仍計費依帳號；撤銷需重新授權。建議優先比較此方案，不將「publish status」誤解成必須公開散布程式 |
| **B：本 PR 的私人 Apps Script + Picker + 固定 producer** | drive.file + 三項明示 script scope；Alert-only Actions-read PAT 留在私人 Script Properties；需標準 Cloud project、私人 Sheet、兩個排程與 pins | 不需購買網域、無公開 web app。可用免費額度，但有六分鐘／容量限制及專案編輯者風險，維護與首輪實測較多；適用小型 package；未來過大必須停用或另審核分片 |
| **C：本機 Python 離線封裝 + 官方 Drive for desktop 同步** | 本程式只寫本機新檔，Google 帳號授權交由官方 desktop client；該 client 的 Drive 存取範圍較廣，不能冒稱自製程式的 drive.file 限制 | 不需自有網域／自建 OAuth 公開 App；需一台開機電腦與本機排程、同步確認及雲端還原校驗；費用取決儲存方案與既有設備，非此 PR 已實作或自動選用 |

若最小權限、指定資料夾或個人授權任一條件無法成立，**停止真實授權／上傳**。不能以 DriveApp 全 Drive 替代，亦不能把新 App 的空 namespace 當成已移交備份。

## 驗收狀態

- 程式：停用驗證版已完成，未部署，原有 Python 工具未改。
- 模擬：Node 核心／Apps Script API adapter tests；Node 只是測試 host，不在 production .gs 中使用 Node APIs。
- 相容：CI 重跑既有 610 項，檢查既有程式 diff 為空，並以真實正式 Artifact 做 Python prepared export；Drive 僅模擬。
- 真正 Google 上傳／下載還原：**未授權、未執行、未驗證**。
- Apps Script 真實 quota／記憶體／六分鐘／八天以上授權持續性：**未驗證**。
- 每日排程／獨立 producer／Google trigger：**未部署、未啟用**。

本地測試與雲端驗收 Artifact 會列出測試紀錄及 envelope。原始策略勝率／RR／資金門檻與交易樣本不因備份而改變。
