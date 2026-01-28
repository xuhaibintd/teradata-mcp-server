from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, HttpUrl


class RestRequest(BaseModel):
    """REST request schema."""

    url: Optional[HttpUrl] = Field(default=None, description="Full REST URL; if omitted, base_url + path will be used")
    path: Optional[str] = Field(default=None, description="Relative path to combine with configured base_url")
    method: str = Field("GET", description="HTTP method such as GET, POST, PUT, DELETE")
    headers: Optional[Dict[str, str]] = Field(default=None, description="HTTP headers")
    params: Optional[Dict[str, Any]] = Field(default=None, description="Query parameters")
    json: Optional[Any] = Field(default=None, description="JSON request body")
    timeout: Optional[float] = Field(default=None, description="Timeout in seconds; falls back to default if not provided")
