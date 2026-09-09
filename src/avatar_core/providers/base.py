"""Контракт провайдера. Всё, что знает про конкретное API, живёт в наследниках."""

from __future__ import annotations

import abc
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from ..basket import AUDIO, IMAGE, VIDEO, Basket
from ..errors import BasketError, JobFailed, PollTimeout, ProviderError
from ..models import JobResult, JobSpec, JobStatus

# Последняя ошибка по ячейке — чтобы обёртка с повторами знала, временная она
# или окончательная, не переписывая сигнатуру _attempt.
_LAST_ERROR: dict[str, Exception] = {}


@dataclass
class Capabilities:
    images: tuple[int, int] = (0, 0)
    audios: tuple[int, int] = (0, 0)
    videos: tuple[int, int] = (0, 0)
    audio_seconds: tuple[float, float] | None = None
    audio_total_seconds: float | None = None
    video_seconds: tuple[float, float] | None = None
    video_total_seconds: float | None = None
    audio_must_be_mono: bool = False
    image_mimes: tuple[str, ...] = ("image/png", "image/jpeg")
    supports_seed: bool = False
    supports_duration: bool = False
    supports_aspect_ratio: bool = False
    aspect_presets: tuple[str, ...] = ()
    concurrency: int = 1
    wrapper_langs: tuple[str, ...] = ("ru",)

    def check(self, basket: Basket) -> list[str]:
        """Список претензий к корзине. Пустой список = всё в порядке."""
        problems: list[str] = []
        for kind, items, bounds in (
            (IMAGE, basket.images, self.images),
            (AUDIO, basket.audios, self.audios),
            (VIDEO, basket.videos, self.videos),
        ):
            lo, hi = bounds
            if len(items) < lo:
                problems.append(f"нужно минимум {lo} {kind}, в корзине {len(items)}")
            if len(items) > hi:
                problems.append(f"допускается максимум {hi} {kind}, в корзине {len(items)}")

        for asset in basket.images:
            if asset.mime not in self.image_mimes:
                problems.append(f"{asset.path.name}: формат {asset.mime} не поддерживается")

        if self.audio_seconds:
            lo, hi = self.audio_seconds
            for asset in basket.audios:
                if not (lo <= asset.duration_s <= hi):
                    problems.append(
                        f"{asset.path.name}: длительность {asset.duration_s:.2f} с вне окна {lo}–{hi} с"
                    )
        if self.audio_total_seconds is not None:
            total = sum(a.duration_s for a in basket.audios)
            if total > self.audio_total_seconds:
                problems.append(
                    f"суммарная длительность аудио {total:.2f} с превышает лимит {self.audio_total_seconds} с"
                )
        if self.video_seconds:
            lo, hi = self.video_seconds
            for asset in basket.videos:
                if not (lo <= asset.duration_s <= hi):
                    problems.append(
                        f"{asset.path.name}: длительность {asset.duration_s:.2f} с вне окна {lo}–{hi} с"
                    )
        if self.video_total_seconds is not None:
            total = sum(a.duration_s for a in basket.videos)
            if total > self.video_total_seconds:
                problems.append(
                    f"суммарная длительность видео {total:.2f} с превышает лимит {self.video_total_seconds} с"
                )
        if self.audio_must_be_mono:
            for asset in basket.audios:
                if asset.info.channels != 1:
                    problems.append(f"{asset.path.name}: требуется моно, а там {asset.info.channels} канала")
        return problems


class AvatarProvider(abc.ABC):
    name: str = "provider"
    capabilities: Capabilities = Capabilities()

    def __init__(self, settings, logger: logging.Logger | None = None) -> None:
        self.settings = settings
        self.log = logger or logging.getLogger(f"avatar.{self.name}")

    # --- обязательное для наследника ------------------------------------

    @abc.abstractmethod
    def submit(self, job: JobSpec) -> str:
        """Поставить задачу, вернуть идентификатор на стороне провайдера."""

    @abc.abstractmethod
    def poll(self, provider_job_id: str) -> tuple[JobStatus, dict]:
        """Опросить статус."""

    @abc.abstractmethod
    def fetch(self, provider_job_id: str, dest: Path, state: dict) -> Path:
        """Скачать результат в dest."""

    def health(self) -> dict:
        return {}

    def classify_failure(self, state: dict) -> JobFailed:
        """Во что превратить отказ задачи. Наследник может отличить временный сбой."""
        return JobFailed(f"Модель вернула ошибку: {state}", provider=self.name, payload=state)

    def close(self) -> None:
        return None

    # --- общее ----------------------------------------------------------

    def validate(self, job: JobSpec, strict: bool = True) -> list[str]:
        problems = self.capabilities.check(job.basket)
        limit = getattr(self.settings, "max_request_mb", 0) or 0
        estimate = job.basket.payload_estimate_mb()
        if limit and estimate > limit:
            problems.append(f"тело запроса ~{estimate} МБ превышает лимит {limit} МБ")
        if problems and strict:
            raise BasketError(f"[{self.name}/{job.cell_id}] " + "; ".join(problems))
        return problems

    def run(self, job: JobSpec, dest_dir: Path) -> JobResult:
        """Задача целиком, с повторами на временных сбоях бэкенда.

        Повторяем только то, что явно помечено как временное: кончившаяся
        память GPU через минуту уже свободна. Отказ по существу запроса
        повторять бессмысленно, он воспроизведётся.
        """
        attempts = int(getattr(self.settings, "job_retries", 0))
        pause = float(getattr(self.settings, "retry_pause_s", 60.0))
        cooldown = float(getattr(self.settings, "cooldown_s", 0.0))

        result = JobResult(cell_id=job.cell_id, provider=self.name)
        for attempt in range(attempts + 1):
            result = self._attempt(job, dest_dir)
            result.attempts = attempt + 1
            transient = getattr(_LAST_ERROR.get(job.cell_id), "retryable", False)
            if result.status is JobStatus.DONE or not transient or attempt >= attempts:
                break
            wait = pause * (attempt + 1)
            self.log.warning(
                "%s: временный сбой бэкенда, ждём %.0f с и повторяем (попытка %d из %d)",
                job.cell_id, wait, attempt + 2, attempts + 1,
            )
            time.sleep(wait)

        if cooldown:
            # Пауза перед следующей задачей: бэкенду нужно время отдать память.
            self.log.debug("%s: пауза %.0f с перед следующей задачей", job.cell_id, cooldown)
            time.sleep(cooldown)
        return result

    def _attempt(self, job: JobSpec, dest_dir: Path) -> JobResult:
        """Одна попытка: поставить, дождаться, скачать."""
        result = JobResult(cell_id=job.cell_id, provider=self.name)
        dest_dir.mkdir(parents=True, exist_ok=True)
        interval = float(getattr(self.settings, "poll_interval_s", 5.0))
        deadline_s = float(getattr(self.settings, "poll_timeout_s", 1800.0))

        t0 = time.time()
        try:
            result.provider_job_id = self.submit(job)
            result.submitted_at = time.time()
            result.submit_seconds = result.submitted_at - t0
            result.status = JobStatus.SUBMITTED
            self.log.info("%s: задача %s поставлена за %.1f с",
                          job.cell_id, result.provider_job_id, result.submit_seconds)

            state: dict = {}
            deadline = time.time() + deadline_s
            heartbeat_every = float(getattr(self.settings, "heartbeat_s", 30.0))
            next_beat = time.time() + heartbeat_every
            while True:
                if time.time() > deadline:
                    raise PollTimeout(
                        f"Задача {result.provider_job_id} не завершилась за {deadline_s:.0f} с",
                        provider=self.name,
                    )
                time.sleep(interval)
                result.poll_count += 1
                status, state = self.poll(result.provider_job_id)
                # Генерация идёт минутами, и молчащая консоль неотличима от зависшей.
                if time.time() >= next_beat:
                    waited = time.time() - result.submitted_at
                    self.log.info(
                        "%s: ждём %s — %.0f с, опросов %d, статус %s",
                        job.cell_id, result.provider_job_id, waited, result.poll_count,
                        _short_status(state),
                    )
                    next_beat = time.time() + heartbeat_every
                if status is JobStatus.DONE:
                    break
                if status is JobStatus.FAILED:
                    raise self.classify_failure(state)
                result.status = JobStatus.RUNNING

            raw = self.fetch(result.provider_job_id, dest_dir / "output.mp4", state)
            result.output_path = raw
            result.finished_at = time.time()
            result.generate_seconds = result.finished_at - result.submitted_at
            result.status = JobStatus.DONE
            _LAST_ERROR.pop(job.cell_id, None)
            self.log.info("%s: готово за %.1f с", job.cell_id, result.generate_seconds)
        except ProviderError as exc:
            result.status = JobStatus.FAILED
            result.error = str(exc)
            result.error_type = type(exc).__name__
            result.error_payload = getattr(exc, "payload", None)
            result.finished_at = time.time()
            _LAST_ERROR[job.cell_id] = exc
            self.log.error("%s: %s", job.cell_id, exc)
            # Наше сообщение — это пересказ. Инженеры бэкенда просят исходный
            # ответ API дословно, поэтому он идёт в консоль и в run.log рядом.
            raw = raw_text(result.error_payload)
            if raw:
                self.log.error("%s: сырой ответ API: %s", job.cell_id, raw)
        except Exception as exc:  # noqa: BLE001 — падение одной ячейки не должно ронять прогон
            result.status = JobStatus.FAILED
            result.error = f"{type(exc).__name__}: {exc}"
            result.error_type = type(exc).__name__
            result.finished_at = time.time()
            _LAST_ERROR[job.cell_id] = exc
            self.log.error("%s: %s: %s", job.cell_id, type(exc).__name__, exc)
            self.log.debug("%s: полный след", job.cell_id, exc_info=True)
        return result


def raw_text(payload: object, limit: int = 4000) -> str:
    """Ответ API как есть, одной строкой. Пусто — если отвечать нечем."""
    if payload is None or payload == "" or payload == {}:
        return ""
    if isinstance(payload, (dict, list)):
        try:
            text = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(payload)
    else:
        text = str(payload)
    return text if len(text) <= limit else text[:limit] + f"… (обрезано, всего {len(text)} симв.)"


def _short_status(state: dict) -> str:
    """Короткая выжимка из ответа поллинга — без простыни JSON в консоли."""
    if not isinstance(state, dict):
        return str(state)[:60]
    for key in ("status", "state", "task_status", "job_status"):
        if key in state:
            bits = [str(state[key])]
            for extra in ("progress", "queue_position", "position", "eta", "eta_seconds"):
                if extra in state:
                    bits.append(f"{extra}={state[extra]}")
            return ", ".join(bits)
    return str(state)[:60]


def pick_status(value: str | None) -> JobStatus:
    """Приводим разнобой в статусах к своему перечислению."""
    text = (value or "").strip().lower()
    if text in {"done", "completed", "complete", "success", "succeeded", "finished", "ready", "ok"}:
        return JobStatus.DONE
    if text in {"failed", "failure", "error", "canceled", "cancelled", "rejected", "moderated"}:
        return JobStatus.FAILED
    return JobStatus.RUNNING
