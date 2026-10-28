# OKX v0.9 合約規格停機修復

- 故障：https://github.com/Jony666/Alert/actions/runs/40269364109
- 最後正式成功：https://github.com/Jony666/Alert/actions/runs/40247022530
- 根因：BCH-USDT-SWAP 官方 tickSz 由原始帳本 `0.1` 變為目前 `0.01`。原安全檢查正確拒絕；不是格式比較的誤報。
- 官方目前規格：https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId=BCH-USDT-SWAP
- 已用原始 Artifact 與 90 組真實公開行情完成本機恢復、移交、完整輸出恢復與同輸入重播預演。原始 ZIP、帳本根節點、歷史 journal、成交、資金與熔斷不變，沒有補造交易。
- `contract-spec-recovery-local.json` 僅為本機實際預演紀錄；不是正式帳本，也不是雲端 CI 成功證明。雲端驗證以本修復分支的 Actions 紀錄及 repair-validation Artifact 為準。
- 程式、28 項修復測試、原始官方證據與操作說明見 `.github/okx-v09-ops/`。
- 限制：失敗當次 snapshot 未保存；尚無官方公告／精確生效時間。正式排程未更新，須獨立修復 PR 審核後才可部署，之後還須驗收真正的 schedule 恢復。
