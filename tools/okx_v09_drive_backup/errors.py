class BackupError(ValueError):
    """Only allowlisted codes leave this boundary; never echo HTTP bodies/URLs or credentials."""
    def __init__(self, code, phase='backup', retryable=False):
        self.code, self.phase, self.retryable = code, phase, retryable
        super().__init__(code)
    def record(self):
        return dict(status='FAILED',code=self.code,phase=self.phase,retryable=self.retryable,
                    backup_verified=False,secrets_logged=False)
