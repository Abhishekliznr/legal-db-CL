class SourceAccessError(Exception):
    """The source rejected the request or access is not authorized."""
    pass


class SourceRateLimitError(SourceAccessError):
    """The source explicitly rate-limited the client."""
    pass


class SourceUnavailableError(SourceAccessError):
    """The source returned a transient 5xx/server failure."""
    pass


class SourceStructureChangedError(Exception):
    """The source page no longer matches the adapter contract."""
    pass
