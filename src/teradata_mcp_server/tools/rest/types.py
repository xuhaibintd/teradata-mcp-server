from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class RestRequest(BaseModel):
    """REST request schema."""

    model_config = ConfigDict(populate_by_name=True)

    url: HttpUrl | None = Field(default=None, description="Full REST URL; if omitted, base_url + path will be used")
    path: str | None = Field(default=None, description="Relative path to combine with configured base_url")
    method: str = "GET"
    headers: dict[str, str] | None = Field(default=None, description="HTTP headers")
    params: dict[str, Any] | None = Field(default=None, description="Query parameters")
    json_body: Any | None = Field(default=None, alias="json", description="JSON request body")
    timeout: float | None = Field(default=None, description="Timeout in seconds; falls back to default if not provided")
