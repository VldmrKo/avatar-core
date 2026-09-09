"""MiniMax H3 Fast Video API v1.2.0, self-hosted.

Особенность инстанса: запросы сериализуются через одну ограниченную очередь,
каждая генерация занимает весь восьмиGPU-воркер. Поэтому concurrency = 1,
а на переполнение очереди отвечаем вежливым ожиданием, а не спамом.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..errors import JobFailed, ProviderError, Rejected, ResourceExhausted
from ..http import ApiClient
from ..models import JobSpec, JobStatus
from .base import AvatarProvider, Capabilities, pick_status

_ID_KEYS = ("id", "video_id", "videoId", "task_id", "taskId", "job_id", "request_id")
_STATUS_KEYS = ("status", "state", "task_status", "job_status")
_URL_KEYS = ("url", "video_url", "download_url", "content_url", "file_url", "result_url")


def _dig(data: dict, keys: tuple[str, ...]) -> str | None:
    """Ищет ключ на верхнем уровне и на один уровень вглубь — API любят заворачивать в data/result."""
    for key in keys:
        value = data.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    for nest in ("data", "result", "video", "response", "payload"):
        inner = data.get(nest)
        if isinstance(inner, dict):
            found = _dig(inner, keys)
            if found:
                return found
    return None


class H3Provider(AvatarProvider):
    name = "h3"
    capabilities = Capabilities(
        images=(0, 9),
        audios=(0, 3),
        videos=(0, 3),
        audio_seconds=(2.0, 15.0),
        audio_total_seconds=15.0,
        video_seconds=(2.0, 15.0),
        video_total_seconds=15.0,
        image_mimes=("image/png", "image/jpeg"),
        supports_seed=True,
        supports_duration=True,
        supports_aspect_ratio=True,
        aspect_presets=("21:9", "16:9", "4:3", "1:1", "3:4", "9:16"),
        concurrency=1,
        wrapper_langs=("ru", "en"),
    )

    def __init__(self, settings, logger=None) -> None:
        super().__init__(settings, logger)
        headers = {"Content-Type": "application/json"}
        if settings.api_key:
            headers["x-api-key"] = settings.api_key
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
        self.submit_path = settings.extra.get("submit_path", "/v1/videos")

    def close(self) -> None:
        self.client.close()

    def health(self) -> dict:
        out: dict = {}
        try:
            out["health"] = self.client.get_json("/health", retries=0)
        except ProviderError as exc:
            out["health_error"] = str(exc)
        try:
            out["config"] = self.client.get_json("/v1/config", retries=0)
        except ProviderError as exc:
            out["config_error"] = str(exc)
        return out

    # --- основной цикл ---------------------------------------------------

    def build_payload(self, job: JobSpec) -> dict:
        """additionalProperties: false — лишний ключ даёт 422, поэтому None-поля выкидываем."""
        payload: dict = {"prompt": job.prompt_rendered or job.prompt_logical}
        duration = job.params.get("duration_seconds")
        if duration is not None:
            # additionalProperties: false, поэтому в тело идут только поля схемы.
            # duration_source и прочая наша служебка остаётся в манифесте.
            payload["duration_seconds"] = int(duration)
        aspect = job.params.get("aspect_ratio")
        if aspect:
            if aspect not in self.capabilities.aspect_presets:
                raise Rejected(
                    f"aspect_ratio '{aspect}' не входит в {self.capabilities.aspect_presets}",
                    provider=self.name,
                )
            payload["aspect_ratio"] = aspect
        seed = job.params.get("seed")
        if seed is not None:
            payload["seed"] = int(seed)
        if job.basket.images:
            payload["reference_images"] = [a.data_uri() for a in job.basket.images]
        if job.basket.audios:
            payload["reference_audios"] = [a.data_uri() for a in job.basket.audios]
        if job.basket.videos:
            payload["reference_videos"] = [a.data_uri() for a in job.basket.videos]
        return payload

    def submit(self, job: JobSpec) -> str:
        payload = self.build_payload(job)
        data = self.client.post_json(self.submit_path, payload)
        job_id = _dig(data, _ID_KEYS)
        if not job_id:
            raise ProviderError(
                f"В ответе нет идентификатора задачи: {json.dumps(data, ensure_ascii=False)[:400]}",
                provider=self.name,
                payload=data,
            )
        return job_id

    def poll(self, provider_job_id: str) -> tuple[JobStatus, dict]:
        data = self.client.get_json(f"/v1/videos/{provider_job_id}")
        return pick_status(_dig(data, _STATUS_KEYS)), data

    # Инстанс не всегда отдаёт память GPU между задачами: следующая генерация
    # падает за шесть секунд с CUDA out of memory при пустой очереди. Наш запрос
    # тут ни при чём, поэтому отделяем это от настоящих отказов модели.
    TRANSIENT_MARKERS = (
        "out of memory", "cuda error", "no output", "returned no output",
        "scheduler", "device-side assert", "nccl",
    )

    def classify_failure(self, state: dict) -> JobFailed:
        text = json.dumps(state, ensure_ascii=False).lower()
        if any(marker in text for marker in self.TRANSIENT_MARKERS):
            reason = "кончилась память GPU" if "out of memory" in text else "сбой бэкенда"
            return ResourceExhausted(
                f"{reason} — это временное, повторяем", provider=self.name, payload=state
            )
        return JobFailed(f"Модель вернула ошибку: {state}", provider=self.name, payload=state)

    def fetch(self, provider_job_id: str, dest: Path, state: dict) -> Path:
        url = _dig(state, _URL_KEYS)
        if url and url.startswith("http"):
            self.client.download(url, dest)
            return dest
        self.client.download(f"/v1/videos/{provider_job_id}/content", dest)
        if dest.stat().st_size < 1024:
            raise ProviderError(
                f"Файл подозрительно мал ({dest.stat().st_size} байт), похоже вернулась ошибка, а не видео",
                provider=self.name,
            )
        return dest
