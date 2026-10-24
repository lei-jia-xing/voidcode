"""Typed optional queries over the runtime's bounded JSON query transport."""


class QueryContractError(ValueError):
    """A query answered, but its answer violated the agreed contract."""
