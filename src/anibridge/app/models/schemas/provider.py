"""Generic provider metadata schemas used across the web API."""

from pydantic import BaseModel, Field

__all__ = ["ProviderMediaMetadata"]


class ProviderMediaMetadata(BaseModel):
    """Provider-agnostic description of a media item."""

    namespace: str = Field(min_length=1)
    key: str = Field(min_length=1)
    title: str | None = None
    poster_url: str | None = None
    external_url: str | None = None
    labels: list[str] | None = None
