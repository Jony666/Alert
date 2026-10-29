/** Source only. No deployment, trigger creation or upload occurs on import. */
var OKX_CONFIG = Object.freeze({
  enabled: false,
  handoffReviewed: false,
  folderId: '1OlXqvYUPTl7sK5Eds71X_3gcRUQv1G-q',
  repo: 'Jony666/Alert',
  study: 'okx-v095-4e85ee42786c6564',
  sourceWorkflow: 'okx-v09-forward-scheduled.yml',
  sourceWorkflowId: 395226876,
  sourceHead: 'f4ff35a088282a37264b82108e32f6c2e31dc28c',
  // Filled only after independent review of the isolated producer deployment.
  exportWorkflow: 'okx-v09-apps-script-export.yml',
  exportWorkflowId: 0,
  exportHead: 'REVIEW_REQUIRED',
  exportBranch: 'master',
  exporterFingerprint: 'REVIEW_REQUIRED',
  configHash: '4e85ee42786c656414f4a038093fcb12ce2516bdb1f5e6cdbe6b3752b578c5fa',
  modelCommit: 'e8863f819f29ef0cf2806ea06157aae7ac3a633e',
  maxBytes: 12 * 1024 * 1024,
  maxExpandedBytes: 40 * 1024 * 1024,
  deadlineMs: 240000,
  minRemainingMs: 30000,
  maxSourceAgeMs: 36 * 3600000,
  maxPages: 20,
  projectNumber: 'USER_SETUP_REQUIRED',
  pickerApiKey: 'USER_SETUP_REQUIRED'
});
