"""Errors that read services raise and each interface translates (HTTP status, MCP ToolError)."""


class NotFoundError(LookupError):
    """The entity, or something within it, doesn't exist. Also raised for another entity's ids,
    so ids can't be probed across entities."""


class InvalidRequestError(ValueError):
    """The arguments are well-formed but not acceptable (an unconfigured horizon, a future week)."""
