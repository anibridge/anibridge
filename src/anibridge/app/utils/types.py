"""Generic types and protocols for type hinting."""

from typing import Any, Protocol

__all__ = ["Comparable"]


class Comparable(Protocol):
    """Protocol for objects that can be compared using <, >, <=, >= operators."""

    def __lt__(self, other: Any, /) -> bool:
        """Return True if this object is less than other."""
        ...

    def __gt__(self, other: Any, /) -> bool:
        """Return True if this object is greater than other."""
        ...

    def __le__(self, other: Any, /) -> bool:
        """Return True if this object is less than or equal to other."""
        ...

    def __ge__(self, other: Any, /) -> bool:
        """Return True if this object is greater than or equal to other."""
        ...
