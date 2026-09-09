"""Kandinsky Avatars (giga_avatar).

Жёсткие рамки: ровно одно фото (JPEG/PNG), ровно одно аудио (WAV mono),
текстовый промпт. Ни длительности, ни формата кадра, ни seed — что выдаст,
то и выдаст. Поэтому формат кадра для всего эксперимента определяется
калибровкой по первой генерации именно этой модели.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

from ..errors import ProviderError
from ..http import ApiClient
from ..models import JobSpec, JobStatus
from .base import AvatarProvider, Capabilities, pick_status

_ID_KEYS = ("task_id", "taskId", "id", "uuid")
_STATUS_KEYS = ("status", "state", "task_status")
_URL_KEYS = ("url", "result", "video", "video_url", "file", "download_url", "link")
_B64_KEYS = ("video", "result", "content", "file", "data", "base64")


def _dig(data: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    for nest in ("data", "result", "response", "payload", "output"):
        inner = data.get(nest)
        if isinstance(inner, dict):
            found = _dig(inner, keys)
            if found:
                return found
    return None


class KandinskyProvider(AvatarProvider):
    name = "kandinsky"
    capabilities = Capabilities(
        images=(1, 1),
        audios=(1, 1),
        videos=(0, 0),
        audio_must_be_mono=True,
        image_mimes=("image/png", "image/jpeg"),
        supports_seed=False,
        supports_duration=False,
        supports_aspect_ratio=False,
        concurrency=2,
        wrapper_langs=("ru",),
    )

    def __init__(self, settings, logger=None) -> None:
        super().__init__(settings, logger)
        headers = {"Content-Type": "application/json"}
        if settings.api_key:
            headers["Authorization"] = f"Bearer {settings.api_key}"
        self.client = ApiClient(
            settings.base_url,
            headers=headers,
            connect_timeout_s=settings.connect_timeout_s,
            read_timeout_s=settings.read_timeout_s,
            retries=settings.submit_retries,
            provider=self.name,
            proxy=getattr(settings, "proxy", ""),
            verify_tls=getattr(settings, "verify_tls", True),
            logger=self.log,
        )
        # Копия, а не правка классового атрибута — иначе настройка утечёт на все экземпляры.
        self.capabilities = replace(type(self).capabilities, concurrency=settings.concurrency or 2)
        self.submit_path = settings.extra.get("submit_path", "/tasks/giga_avatar")

    def close(self) -> None:
        self.client.close()

    def health(self) -> dict:
        # Отдельного health у сервиса нет. Дешёвая проверка — статус заведомо
        # несуществующей задачи с числовым id: сервер ответит 404 (или 400),
        # и этого достаточно, чтобы отличить «не пускает по ключу» от «не отвечает».
        try:
            self.client.get_json("/tasks/0", retries=0)
            return {"reachable": True, "auth_ok": True}
        except ProviderError as exc:
            # Ключевое различие: сервер ответил хоть каким-то кодом — значит он
            # доступен, пусть и ругается. Нет кода вообще — это сеть, и сервис
            # недоступен. Раньше таймаут проходил как «доступен», что вводило в
            # заблуждение ровно там, где нужна правда.
            answered = exc.status_code is not None
            return {
                "reachable": answered,
                "auth_ok": answered and exc.status_code not in (401, 403),
                "detail": str(exc),
            }

    # --- основной цикл ---------------------------------------------------

    def build_payload(self, job: JobSpec) -> dict:
        image = job.basket.images[0]
        audio = job.basket.audios[0]
        return {
            "censor": bool(job.params.get("censor", False)),
            "params": {
                "image": image.b64(),
                "audio": audio.b64(),
                "query": job.prompt_rendered or job.prompt_logical,
            },
        }

    def submit(self, job: JobSpec) -> str:
        data = self.client.post_json(self.submit_path, self.build_payload(job))
        task_id = _dig(data, _ID_KEYS)
        if not task_id:
            raise ProviderError(
                f"В ответе нет task_id: {json.dumps(data, ensure_ascii=False)[:400]}",
                provider=self.name,
                payload=data,
            )
        return task_id

    def poll(self, provider_job_id: str) -> tuple[JobStatus, dict]:
        data = self.client.get_json(f"/tasks/{provider_job_id}")
        return pick_status(_dig(data, _STATUS_KEYS)), data

    def fetch(self, provider_job_id: str, dest: Path, state: dict) -> Path:
        """Документация не уточняет, что именно отдаёт /result. Разбираем три варианта."""
        resp = self.client.get_raw(f"/tasks/{provider_job_id}/result")
        content_type = resp.headers.get("content-type", "")

        if content_type.startswith(("video/", "application/octet-stream")):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.content)
            return dest

        try:
            data = resp.json()
        except ValueError:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.content)
            return dest

        if not isinstance(data, dict):
            data = {"_list": data}

        url = _dig(data, _URL_KEYS)
        if url and str(url).startswith("http"):
            self.client.download(url, dest)
            return dest

        blob = _dig(data, _B64_KEYS)
        if blob and len(blob) > 1024:
            payload = blob.split(",", 1)[-1] if blob.startswith("data:") else blob
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(base64.b64decode(payload))
            return dest

        raise ProviderError(
            "Не удалось понять формат ответа /result: "
            f"{json.dumps(data, ensure_ascii=False)[:400]}",
            provider=self.name,
            payload=data,
        )
