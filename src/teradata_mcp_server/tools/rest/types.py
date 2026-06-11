from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from .endpoints import endpoint_names

_ENDPOINT_NAMES = endpoint_names()
_ENDPOINT_DESCRIPTION = (
    "Logical endpoint name loaded from rest_config.yml. Use this instead of method/path. "
    f"Allowed values: {', '.join(_ENDPOINT_NAMES)}"
)


class RestRequest(BaseModel):
    """REST request schema."""

    model_config = ConfigDict(populate_by_name=True)

    endpoint: str | None = Field(
        default=None,
        description=_ENDPOINT_DESCRIPTION,
        json_schema_extra={"enum": [*_ENDPOINT_NAMES, None]},
    )
    path_params: dict[str, Any] = Field(
        default_factory=dict,
        description="Values for placeholders in the configured path, such as objectID, templateID, reqID, or groupID",
    )
    url: HttpUrl | None = Field(
        default=None,
        description="Legacy absolute URL. Disabled unless allow_absolute_urls is enabled in rest_config.yml",
    )
    path: str | None = Field(
        default=None,
        description="Legacy configured relative path. Prefer endpoint plus path_params",
    )
    method: str | None = Field(default=None, description="Legacy HTTP method. Prefer the configured endpoint method")
    headers: dict[str, str] | None = Field(default=None, description="HTTP headers")
    params: dict[str, Any] | None = Field(default=None, description="Query parameters")
    json_body: Any | None = Field(default=None, alias="json", description="JSON request body")
    timeout: float | None = Field(default=None, description="Timeout in seconds; falls back to default if not provided")
