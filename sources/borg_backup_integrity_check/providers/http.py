"""Small HTTP helper shared by REST-based providers: auth header, retries, rate limits, masking."""

from __future__ import annotations

import time
from typing import Any

import requests

from ..errors import ProviderError
from ..logging_setup import get_logger
from ..redaction import register_secret

log = get_logger("providers.http")


class JsonApi:
    def __init__(
        self,
        base_url: str,
        token: str,
        user_agent: str = "borg-backup-integrity-check",
        timeout: int = 60,
        retries: int = 4,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "User-Agent": user_agent,
                "Accept": "application/json",
            }
        )
        self.timeout = timeout
        self.retries = retries
        register_secret(token)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        ok: tuple[int, ...] = (200, 201, 202, 204),
    ) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.session.request(
                    method, url, params=params, json=json, timeout=self.timeout
                )
            except requests.RequestException as exc:
                if attempt <= self.retries:
                    delay = min(2**attempt, 30)
                    log.warning(
                        "%s %s failed (%s); retrying in %ss",
                        method,
                        path,
                        exc.__class__.__name__,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise ProviderError(
                    f"{method} {path}: network error: {exc.__class__.__name__}", retryable=True
                ) from exc
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt <= self.retries:
                    delay = _retry_delay(resp, attempt)
                    log.warning(
                        "%s %s -> %s; retrying in %.0fs", method, path, resp.status_code, delay
                    )
                    time.sleep(delay)
                    continue
                raise ProviderError(
                    f"{method} {path}: HTTP {resp.status_code} after {attempt} attempts",
                    status=resp.status_code,
                    retryable=True,
                )
            if resp.status_code not in ok:
                raise ProviderError(
                    f"{method} {path}: HTTP {resp.status_code}: {_error_message(resp)}",
                    status=resp.status_code,
                    retryable=False,
                )
            if resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError as exc:
                raise ProviderError(f"{method} {path}: invalid JSON response") from exc

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, json: Any = None, **kw: Any) -> Any:
        return self.request("POST", path, json=json, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)

    def paginate(
        self,
        path: str,
        key: str,
        params: dict[str, Any] | None = None,
        per_page: int = 50,
        page_param: str = "page",
        per_page_param: str = "per_page",
        max_pages: int = 100,
    ) -> list[Any]:
        """Generic page-number pagination; stops when a page returns fewer than ``per_page`` items."""
        params = dict(params or {})
        params[per_page_param] = per_page
        items: list[Any] = []
        for page in range(1, max_pages + 1):
            params[page_param] = page
            data = self.get(path, params=params) or {}
            chunk = data.get(key, []) or []
            items.extend(chunk)
            if len(chunk) < per_page:
                break
        return items


def _retry_delay(resp: requests.Response, attempt: int) -> float:
    for header in ("Retry-After", "retry-after"):
        if header in resp.headers:
            try:
                return min(float(resp.headers[header]), 120.0)
            except ValueError:
                pass
    reset = resp.headers.get("RateLimit-Reset") or resp.headers.get("ratelimit-reset")
    if reset:
        try:
            return max(1.0, min(float(reset) - time.time(), 120.0))
        except ValueError:
            pass
    return float(min(2**attempt, 30))


def _error_message(resp: requests.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
        if isinstance(err, str):
            return err
        return str(data.get("message") or data)[:300]
    return str(data)[:300]
