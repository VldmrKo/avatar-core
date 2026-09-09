"""Kandinsky 6 I2I: фотография → рисованный аватар.

Живёт в ядре, а не в стенде и не в мини-аппе, потому что нужен обоим:
стенд прогоняет по нему корзинку лиц и сравнивает, мини-апп делает из него
первый шаг «мультяшного аватара». Один клиент — одно место, где чинить.

Протокол асинхронный, как и у остальных: POST создаёт задачу, GET отдаёт
статус, отдельный GET — результат.

    POST /tasks/k6-i2i   {"censor": bool,
                          "params": {"image": [base64, до трёх],
                                     "query": str,
                                     "beautificator": "enabled"|"disabled"}}
    GET  /tasks/{id}          {"status": "..."}
    GET  /tasks/{id}/result

Про `beautificator`: это переписывание промпта силами LLM на стороне сервиса.
В продукте он выключен намеренно — промпт у нас зашит и не редактируется,
значит должен вести себя одинаково от запуска к запуску, а лишний
недетерминированный слой этому мешает.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path

from .errors import ProviderError
from .http import ApiClient

log = logging.getLogger("avatar.toonify")

SUBMIT_PATH = "/tasks/k6-i2i"

_ID_KEYS = ("task_id", "id", "taskId")
_STATUS_KEYS = ("status", "state", "task_status")
_URL_KEYS = ("url", "image_url", "download_url", "result_url", "file_url")
_B64_KEYS = ("image", "images", "result", "data", "b64", "base64", "content")

DONE = {"done", "success", "succeeded", "completed", "finished", "ready"}
FAILED = {"failed", "error", "cancelled", "canceled", "rejected"}


def dig(data: dict, keys: tuple[str, ...]) -> str | None:
    """Ключ на верхнем уровне или на уровень глубже: API любят заворачивать
    полезное в data/result, а списки отдавать вместо строк."""
    for key in keys:
        value = data.get(key)
        if isinstance(value, list) and value:
            value = value[0]
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    for nest in ("data", "result", "response", "payload", "output"):
        inner = data.get(nest)
        if isinstance(inner, dict):
            found = dig(inner, keys)
            if found:
                return found
        if isinstance(inner, list) and inner and isinstance(inner[0], dict):
            found = dig(inner[0], keys)
            if found:
                return found
    return None


class Toonify:
    """Один клиент на серию картинок: соединение переиспользуется."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        connect_timeout_s: float = 15.0,
        read_timeout_s: float = 180.0,
        retries: int = 3,
        poll_interval_s: float = 3.0,
        poll_timeout_s: float = 600.0,
        proxy: str = "",
        verify_tls: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        if not base_url:
            raise ProviderError("Не задан адрес Kandinsky API", provider="kandinsky")
        self.log = logger or log
        self.poll_interval_s = poll_interval_s
        self.poll_timeout_s = poll_timeout_s
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.client = ApiClient(
            base_url,
            headers=headers,
            connect_timeout_s=connect_timeout_s,
            read_timeout_s=read_timeout_s,
            retries=retries,
            provider="kandinsky",
            proxy=proxy,
            verify_tls=verify_tls,
            logger=self.log,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Toonify:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- один снимок -----------------------------------------------------

    def run(self, src: Path, dest: Path, query: str, *,
            beautificator: str = "disabled", censor: bool = False,
            label: str = "") -> dict:
        """Фото → рисунок. Возвращает id задачи и потраченное время."""
        started = time.time()
        task_id = self.submit(src, query, beautificator=beautificator, censor=censor)
        self.log.info("%s: рисунок, задача %s", label or src.name, task_id)
        self.wait(task_id, label=label or src.name)
        self.fetch(task_id, dest)
        return {"task_id": task_id, "seconds": round(time.time() - started, 1)}

    def submit(self, src: Path, query: str, *,
               beautificator: str = "disabled", censor: bool = False) -> str:
        payload = {
            "censor": censor,
            "params": {
                # Схема принимает до трёх файлов. Мы шлём одно фото: смешивать
                # лица в аватаре незачем, а лишний вход — лишняя переменная.
                "image": [base64.b64encode(src.read_bytes()).decode("ascii")],
                "query": query,
                "beautificator": beautificator,
            },
        }
        data = self.client.post_json(SUBMIT_PATH, payload)
        task_id = dig(data, _ID_KEYS)
        if not task_id:
            raise ProviderError(
                f"В ответе нет task_id: {json.dumps(data, ensure_ascii=False)[:400]}",
                provider="kandinsky", payload=data,
            )
        return task_id

    def wait(self, task_id: str, *, label: str = "") -> dict:
        deadline = time.time() + self.poll_timeout_s
        last = ""
        while time.time() < deadline:
            data = self.client.get_json(f"/tasks/{task_id}")
            status = (dig(data, _STATUS_KEYS) or "").lower()
            if status != last:
                self.log.info("%s: %s", label or task_id, status or "без статуса")
                last = status
            if status in DONE:
                return data
            if status in FAILED:
                raise ProviderError(
                    f"Задача {task_id} отклонена: "
                    f"{json.dumps(data, ensure_ascii=False)[:400]}",
                    provider="kandinsky", payload=data,
                )
            time.sleep(self.poll_interval_s)
        raise ProviderError(
            f"Задача {task_id} не завершилась за {self.poll_timeout_s:.0f} с",
            provider="kandinsky",
        )

    def fetch(self, task_id: str, dest: Path) -> Path:
        """Документация не уточняет формат /result — разбираем три варианта:
        сырые байты, ссылку и base64. Та же развилка, что у видео-провайдера."""
        resp = self.client.get_raw(f"/tasks/{task_id}/result")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if resp.headers.get("content-type", "").startswith(
            ("image/", "application/octet-stream")
        ):
            dest.write_bytes(resp.content)
            return dest
        try:
            data = resp.json()
        except ValueError:
            dest.write_bytes(resp.content)
            return dest
        if not isinstance(data, dict):
            data = {"_list": data}
        url = dig(data, _URL_KEYS)
        if url and str(url).startswith("http"):
            self.client.download(url, dest)
            return dest
        blob = dig(data, _B64_KEYS)
        if blob and len(blob) > 1024:
            raw = blob.split(",", 1)[-1] if blob.startswith("data:") else blob
            dest.write_bytes(base64.b64decode(raw))
            return dest
        raise ProviderError(
            f"Не понял формат /result: {json.dumps(data, ensure_ascii=False)[:400]}",
            provider="kandinsky", payload=data,
        )
