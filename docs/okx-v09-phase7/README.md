# OKX v0.9 第七階段：繁中報表、完整性與本機封存

本階段是獨立的離線唯讀工具，不修改策略、風控、正式前瞻排程、Render、推播或下單。`user-provided-example.md` 是使用者附件原文，僅作版面與欄位參考；`example-report.md` 則是由實際正式 Artifact 驗證產生，沒有手工填入交易或勝率。

## 實際整合來源

- 研究程式／全部既有測試固定來源：`e8863f819f29ef0cf2806ea06157aae7ac3a633e`。
- freeze／genesis anchor：`104e6acf2096f9fc507f45395aff7c82986f1207`，相同 bytes 複製至工具 registry 並釘選 SHA-256。
- 正式排程控制基準：`f4ff35a088282a37264b82108e32f6c2e31dc28c`，本階段必須保持該工作流程逐位元不變。
- 規格修復操作：`f2f56aed416d540a71156dd58f5bc9a28a6a0baf`，只重跑其測試，不修改或部署。
- 實際驗收來源 revision 20：run [40851188958](https://github.com/Jony666/Alert/actions/runs/40851188958)、Artifact `12886458799`、ZIP SHA-256 `f0892dd6befc4ae1574a263f8a9624aa9856d64947449d9ff1c1dc13b62830d3`。
- 直接 parent revision 19：run [40805798260](https://github.com/Jony666/Alert/actions/runs/40805798260)、Artifact `12868550750`。

固定這兩份已存在的正式資料，讓 CI 可重現且不受行情／新增排程輪次影響。它們不是「永遠最新」；要分析較新的同版本 Artifact，請提供那份 ZIP 及對應 GitHub API context，不挑選獲利期間。附檔只有示範 Markdown，未包含另外的備份工具原始碼，因此整合基礎為既有 `v09.forward.report/state` 與現行 workflow 中的匯出工具。

## 可產生的內容

- `report.md`、無 JavaScript／外部資源的 `report.html`、獨立 `equity.svg`。
- 逐筆已平倉交易、分批 entry／exit 成交、TP1／TP2、原始／保護停損、退出原因、手續費、資金費、內含滑價成本與實現淨 R。
- 未平倉與待成交資料；策略統計、觀察勝率與 95% Wilson 區間、平均獲利／虧損、PF、持倉平均／中位數／P90／P95／最長與 1～3 天比例。
- `trades.csv`、`fills.csv`、原始 `source-trades.csv`／`equity.csv`、positions／pending JSON、完整 input inventory、來源 context、`metrics.json`、逐檔 SHA-256 的 `report-manifest.json`。
- 原 journal 的初始化／完成／規格移交版本事件，來源 run、Artifact、模型、freeze、操作 payload 與報表程式指紋。

計畫淨 RR、平均實現獲利／虧損比、逐筆淨 R 與 PF 分開顯示。入場計畫值照原帳本，不用遠端目標或回測報酬重新推算。零交易時勝率與盈虧比顯示未驗證，CSV 只有欄名，沒有示範成交。非零數值分支只用明確標示的單元測試驗證；合成資料不能通過真實前瞻報表入口。

## 完整 Artifact 驗證

1. 完整 ZIP 的外部 SHA-256／大小，對應成功 GitHub run、workflow、master、固定控制版本與 Artifact 名稱／ID。
2. ZIP 路徑、大小、壓縮比、成員數、symlink、加密、重複／大小寫衝突與非正規路徑防護；不執行 Artifact 內的任何程式。
3. 目前 profile 的 **22 個檔案全部**列入 inventory；資料檔核對原 manifest，freeze／anchor／修復程式等固定資產另釘選版本雜湊，操作 receipt 逐項核對。
4. 獨立 registry 與凍結程式每個來源模組的 SHA-256 驗證後，才載入既有 state／report 函式；事件鏈、根節點、風控狀態與現金對帳必須通過。
5. snapshot 的真實來源、確認行情、90 組 metadata／checksum；positions、summary、CSV 與 state 的語意勾稽。JSON 重複鍵及非有限數值拒絕。CSV 數值以 Decimal 精確比較；summary 的整數／字串精確比對，浮點金額使用絕對 1e-6／相對 1e-12 的捨入容差，不改寫來源值。
6. 成交數量、平均入場價、5 bps 模擬費率、費用池、資金費 journal、實現淨損益／淨 R、持倉時間、curve 權益加總均核對。滑價在成交價中，不重複扣除。
7. 可選直接 parent ZIP／context：驗證 revision、run、Artifact ID、parent state hash、journal／成交前綴與 watermark。未提供時明示未驗證該 Artifact parent，不虛稱全部祖先封存都存在。
8. 驗證後物件如被修改，產報／備份前會再核對封印，拒絕混入未驗證內容。

SHA-256 不等於 GitHub 數位簽章；context 必須由可信 API／既有封存取得。profile 目前只接受已審核的 scheduled spec-review v1 格式。未來工作流程／格式有變更時需獨立版本審核，不會悄悄接受。

## 執行方式（Python 3.12、Linux／WSL）

先 checkout 固定研究程式到獨立目錄並安裝其 `okx-monitor/backtest/requirements.txt`。不要修改正式執行中的研究資料。工具本身使用標準函式庫；既有驗證器需要凍結程式的 pandas／numpy／requests。範例以專案根目錄執行：

```bash
export PYTHONPATH="$PWD/tools"
python -m okx_v09_reporting verify artifact.zip \
  --source-context source-context.json --model-root /path/to/frozen/okx-monitor/backtest \
  --parent-artifact parent.zip --parent-context parent-context.json

python -m okx_v09_reporting report artifact.zip \
  --source-context source-context.json --model-root /path/to/frozen/okx-monitor/backtest \
  --out /path/to/new-report-directory

python -m okx_v09_reporting backup artifact.zip \
  --source-context source-context.json --model-root /path/to/frozen/okx-monitor/backtest \
  --out /path/to/user-controlled-local-vault
```

只有明確呼叫 `.github/scripts/phase7_fetch_source.py` 才會讀取 GitHub：它只 GET 既存 Artifact／run，不寫入正式帳本或上傳備份。需既有 GitHub 權限，**不需要任何 OKX Secret**。主報表與備份模組完全沒有網路傳輸功能。

context schema 1 含 `repository`、GitHub Artifact 的 `id/name/digest/size_in_bytes/created_at/expires_at/expired/workflow_run`，及 run 的 `id/workflow_id/head_branch/head_sha/event/status/conclusion/run_attempt/path/created_at`。不要自行編造元資料來略過來源驗證。

## 本機備份、seal 與恢復

備份按 `study/revision/run/ZIP-SHA256` 儲存完整原 ZIP、context、22 檔 inventory、工具版本與 sealed manifest。`backup-index.jsonl` 只追加，既有 revision 不允許不同內容替換；新增版本會檢查 journal 前綴與可取得的直接 parent。較舊版本可補存但不取代最新版本。相同來源重複備份為冪等操作。

建立成功時輸出的 `backup_manifest_sha256` 必須另外保存，建議兩份使用者控制的離線副本。恢復時不要拿「目前重算的 hash」冒充原先 seal：

```bash
python -m okx_v09_reporting restore /path/to/specific-backup \
  --backup-sha256 ORIGINAL_SAVED_SEAL \
  --model-root /path/to/frozen/okx-monitor/backtest \
  --out /path/to/new-offline-restored-directory
```

恢復會驗證 seal、所有檔案、原 ZIP 與完整帳本，並只寫入**不存在的新目錄**，不覆寫任何正式或既有資料。恢復後的原 ZIP 及每個 bundle 檔案必須逐位元一致。備份 writer 使用鎖與 no-replace 原子提交；中斷或索引損壞安全停止，不重新建立空白帳本。完整目錄已提交但 index 尚未追加的孤兒，可在重新核對精確來源後補入索引，不改寫備份。

這是離線封存恢復，**不是**將該版本選為正式最新帳本。歷史 ZIP 即使超過 GitHub 90 天保留或正式引擎 7 天新鮮度，仍可離線檢視；報表會警示，但永不授權正式恢復，也沒有修改原停機門檻。

## 自動備份設計：目前停用

`backup-plan.json` 預設 `enabled=false`、`destination=null`、`destination_confirmed=false`、`external_transfer_enabled=false`：

```bash
python -m okx_v09_reporting backup-plan --config docs/okx-v09-phase7/backup-plan.json
```

目前只回報停用狀態，不新增任何排程或傳輸。主程式沒有 S3、Google Drive、FTP、郵件、webhook 等外部上傳實作；外部傳輸旗標即使改成 true 也會拒絕。

目的地確認後的候選方案：由獨立唯讀備份作業下載既有累計 Artifact → 完整驗證 → 依版本原子寫入已確認的本機儲存區 → 驗證 seal／定期恢復演練 → 保存兩份離線副本及期限清單。啟用、外部儲存選型、權限與加密都須另外審核，本階段不接入正式研究 workflow。既有每 30 天匯出規則維持，90 天 GitHub Artifact 不是永久備份。

## 測試與 CI 邊界

新分支 workflow 只有本分支 push 觸發，沒有 schedule；權限僅 contents/read、actions/read。它跑全部凍結測試、既有 28 項規格修復與 17 項狀態移交測試、新報表／備份測試，以及真實 revision 19／20 的報表、重複備份、封印與新目錄恢復驗收。

CI 的 `phase7-report-validation-*` 僅是既有 GitHub 上的開發驗收輸出，不是已啟用的外部長期備份，也絕不使用 production `forward-state-*` 名稱。報告 Artifact 不會被正式排程當作恢復來源；正式帳本、排程、策略與 Render 均維持原樣。PR #1、PR #2 及 master 都不合併。
