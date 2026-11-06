class TransientError(Exception):
    """Retryable processing failure (e.g. DB locked, downstream timeout)."""


class PermanentError(Exception):
    """Non-retryable processing failure (poison message). Goes straight to the DLQ."""
