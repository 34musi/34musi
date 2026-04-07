"""Selector-specific errors."""


class DataSourceError(RuntimeError):
    """Raised when a market data source cannot serve a request."""
