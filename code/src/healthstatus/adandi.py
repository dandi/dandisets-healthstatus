from __future__ import annotations
from collections.abc import AsyncGenerator
from dataclasses import InitVar, dataclass, field
from datetime import datetime
import platform
import sys
from typing import Any
from anyio.abc import AsyncResource
import httpx
from pydantic import BaseModel
from .aioutil import arequest

if sys.version_info[:2] >= (3, 10):
    from contextlib import aclosing
else:
    from async_generator import aclosing

USER_AGENT = "dandisets-healthstatus ({}) httpx/{} {}/{}".format(
    "https://github.com/dandi/dandisets-healthstatus",
    httpx.__version__,
    platform.python_implementation(),
    platform.python_version(),
)


@dataclass
class AsyncDandiClient(AsyncResource):
    api_url: str
    token: InitVar[str | None] = None
    session: httpx.AsyncClient = field(init=False)

    def __post_init__(self, token: str | None) -> None:
        headers = {"User-Agent": USER_AGENT}
        if token is not None:
            headers["Authorization"] = f"token {token}"
        self.session = httpx.AsyncClient(
            base_url=self.api_url,
            headers=headers,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self.session.aclose()

    def get_url(self, path: str) -> str:
        if path.lower().startswith(("http://", "https://")):
            return path
        else:
            return self.api_url.rstrip("/") + "/" + path.lstrip("/")

    async def get(self, path: str, **kwargs: Any) -> Any:
        return (await arequest(self.session, "GET", path, **kwargs)).json()

    async def paginate(
        self,
        path: str,
        page_size: int | None = None,
        params: dict | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator:
        """
        Paginate through the resources at the given path: GET the path, yield
        the values in the ``"results"`` key, and repeat with the URL in the
        ``"next"`` key until it is ``null``.
        """
        if page_size is not None:
            if params is None:
                params = {}
            params["page_size"] = page_size
        r = await self.get(path, params=params, **kwargs)
        while True:
            for item in r["results"]:
                yield item
            if r.get("next"):
                r = await self.get(r["next"], **kwargs)
            else:
                break

    async def get_dandiset(self, dandiset_id: str) -> DandisetInfo:
        return DandisetInfo.from_raw_response(
            await self.get(f"/dandisets/{dandiset_id}/")
        )

    async def get_dandisets(self) -> AsyncGenerator[DandisetInfo, None]:
        async with aclosing(self.paginate("/dandisets/")) as ait:
            async for data in ait:
                yield DandisetInfo.from_raw_response(data)

    async def get_asset_paths(self, dandiset_id: str) -> AsyncGenerator[str, None]:
        async with aclosing(
            self.paginate(
                f"/dandisets/{dandiset_id}/versions/draft/assets/",
                params={"order": "created", "page_size": "1000"},
            )
        ) as ait:
            async for item in ait:
                yield item["path"]


@dataclass
class DandisetInfo:
    identifier: str
    draft_modified: datetime

    @classmethod
    def from_raw_response(cls, data: dict[str, Any]) -> DandisetInfo:
        resp = DandisetResponse.model_validate(data)
        return cls(
            identifier=resp.identifier, draft_modified=resp.draft_version.modified
        )


class VersionInfo(BaseModel):
    modified: datetime


class DandisetResponse(BaseModel):
    identifier: str
    draft_version: VersionInfo
