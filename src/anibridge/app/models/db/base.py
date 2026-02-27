"""Base Model Module."""

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy.orm import DeclarativeBase

from anibridge.app.exceptions import UnsupportedModeError

__all__ = ["Base"]

if TYPE_CHECKING:
    from pydantic.main import IncEx


def _generic_serialize(obj: Any) -> Any:
    """Recursively convert an object to a JSON-serializable format.

    Args:
        obj: The object to convert.

    Returns:
        A JSON-serializable representation of the object.
    """
    if obj is None:
        return None
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


class Base(DeclarativeBase):
    """Base class for all database models."""

    def model_dump(
        self,
        *,
        mode: Literal["json", "python"] | str = "python",
        include: IncEx | None = None,
        exclude: IncEx | None = None,
        exclude_none: bool = False,
    ) -> dict[str, Any]:
        """Dump the model fields to a dictionary.

        Imitates the behavior of Pydantic's model_dump method.
        """
        if not exclude_none and not include and not exclude:
            result = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        else:
            inc = set(include) if include and not isinstance(include, dict) else include
            exc = set(exclude) if exclude and not isinstance(exclude, dict) else exclude

            result = {}
            for k, v in self.__dict__.items():
                if k.startswith("_"):
                    continue
                if exclude_none and v is None:
                    continue
                if inc and k not in inc:
                    continue
                if exc and k in exc:
                    continue
                result[k] = v

        if mode == "python":
            return result
        if mode == "json":
            return json.loads(json.dumps(result, default=_generic_serialize))
        raise UnsupportedModeError(f"Unsupported mode: {mode}")
