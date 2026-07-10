"""Custom exception types for the Canonical Entity Atlas."""


class CEAError(Exception):
    """Base error for CEA operations."""


class BudoIdError(CEAError):
    """Raised for BUDO ID generation or parsing issues."""


class CEANotFoundError(CEAError):
    """Raised when an entity cannot be resolved in the identity store."""


class CEADuplicateError(CEAError):
    """Raised when a conflicting entity already exists."""
