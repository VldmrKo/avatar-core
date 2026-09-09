"""HTTP-клиент: ретраи, таймауты и маскировка секретов в логах.

Секрет не должен попасть в лог никогда — поэтому маскировка живёт здесь,
на единственном пути наружу, а не в каждом адаптере.
"""

from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Any

import httpx

from .errors import (
    AuthError,
    ConfigError,
    QueueFull,
    RateLimited,
    Rejected,
    TransportError,
)

SENSITIVE_HEADERS = {"authorization", "x-api-key", "api-key", "cookie"}


def _check_header_charset(headers: dict[str, str], provider: str) -> None:
    """HTTP-заголовки — только latin-1. Кириллица там даёт нечитаемый срыв в httpx."""
    for key, value in headers.items():
        try:
            value.encode("latin-1")
        except UnicodeEncodeError:
            raise ConfigError(
                f"Заголовок {key} провайдера '{provider}' содержит символы вне latin-1. "
                "Скорее всего в ключ из secrets.env попал комментарий или русский текст."
            ) from None


def mask(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "…" + value[-3:] if len(value) > 12 else "…"


def safe_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: (mask(v) if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}


class ApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        connect_timeout_s: float = 15.0,
        read_timeout_s: float = 180.0,
        retries: int = 4,
        provider: str = "",
        proxy: str = "",
        verify_tls: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        self.provider = provider
        self.retries = retries
        self.log = logger or logging.getLogger(f"avatar.http.{provider or 'api'}")
        self._headers = headers or {}
        _check_header_charset(self._headers, provider)
        # proxy пустой -> httpx возьмёт HTTP(S)_PROXY из окружения сам.
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=self._headers,
            timeout=httpx.Timeout(read_timeout_s, connect=connect_timeout_s),
            follow_redirects=True,
            verify=verify_tls,
            **({"proxy": proxy} if proxy else {}),
        )
        self.log.debug("клиент %s -> %s, заголовки %s", provider, base_url, safe_headers(self._headers))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- низкий уровень -------------------------------------------------

    def _classify(self, resp: httpx.Response) -> None:
        code = resp.status_code
        if code < 400:
            return
        body = resp.text[:600]
        common = {"provider": self.provider, "status_code": code, "payload": body}
        if code in (401, 403):
            raise AuthError("Авторизация не прошла. Проверьте ключ в secrets.env", **common)
        if code == 422:
            raise Rejected(f"Запрос отклонён валидацией: {body}", **common)
        if code == 429:
            raise RateLimited("Слишком часто, ждём", **common)
        if code in (409, 503, 502, 504):
            raise QueueFull(f"Сервис занят или недоступен: {body}", **common)
        if code >= 500:
            raise TransportError(f"Ошибка на стороне сервиса: {body}", **common)
        raise Rejected(f"HTTP {code}: {body}", **common)

    def request(self, method: str, path: str, *, retries: int | None = None, **kwargs: Any) -> httpx.Response:
        attempts = self.retries if retries is None else retries
        last: Exception | None = None
        for attempt in range(attempts + 1):
            try:
                resp = self._client.request(method, path, **kwargs)
                self._classify(resp)
                return resp
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last = TransportError(f"Сеть: {exc}", provider=self.provider)
            except (RateLimited, QueueFull, TransportError) as exc:
                last = exc
            except Exception:
                raise
            if attempt < attempts:
                pause = min(60.0, (2 ** attempt) * 2.0) + random.uniform(0, 1.5)
                self.log.warning(
                    "%s %s — попытка %d/%d не удалась (%s), пауза %.1f с",
                    method, path, attempt + 1, attempts + 1, last, pause,
                )
                time.sleep(pause)
        assert last is not None
        raise last

    # --- удобные обёртки ------------------------------------------------

    def get_json(self, path: str, **kwargs: Any) -> dict:
        resp = self.request("GET", path, **kwargs)
        return _as_json(resp)

    def post_json(self, path: str, payload: dict, **kwargs: Any) -> dict:
        resp = self.request("POST", path, json=payload, **kwargs)
        return _as_json(resp)

    def download(self, path: str, dest: Path, **kwargs: Any) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._client.stream("GET", path, **kwargs) as resp:
            self._classify(resp)
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
        return dest

    def get_raw(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)


def _as_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {"_raw_text": resp.text[:2000], "_content_type": resp.headers.get("content-type", "")}
    return data if isinstance(data, dict) else {"_list": data}
