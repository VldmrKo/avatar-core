"""Единицы работы: что запускаем и что получилось."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .basket import Basket


class JobStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class JobSpec:
    """Одна ячейка матрицы."""

    cell_id: str
    provider: str
    person_id: str
    text_id: str
    form: str            # plain | scene
    wrapper: str         # язык обёртки промпта: ru | en
    basket: Basket
    params: dict[str, Any] = field(default_factory=dict)
    prompt_logical: str = ""    # реплика — одинаковая у всех
    prompt_rendered: str = ""   # то, что реально уйдёт в модель

    def describe(self) -> dict:
        return {
            "cell_id": self.cell_id,
            "provider": self.provider,
            "person_id": self.person_id,
            "text_id": self.text_id,
            "form": self.form,
            "wrapper": self.wrapper,
            "params": self.params,
            "prompt_logical": self.prompt_logical,
            "prompt_rendered": self.prompt_rendered,
            # Что реально ушло референсами. Форма может урезать корзину,
            # и по одному промпту этого не видно.
            "refs": {
                "images": [a.path.name for a in self.basket.images],
                "audios": [a.path.name for a in self.basket.audios],
                "videos": [a.path.name for a in self.basket.videos],
            },
            "payload_mb_estimate": self.basket.payload_estimate_mb(),
        }


@dataclass
class JobResult:
    cell_id: str
    provider: str
    status: JobStatus = JobStatus.PENDING
    provider_job_id: str = ""
    output_path: Path | None = None
    share_path: Path | None = None
    preview_path: Path | None = None
    error: str = ""
    error_type: str = ""
    error_payload: object = None   # сырой ответ API как есть — его спрашивают инженеры
    submitted_at: float = 0.0
    finished_at: float = 0.0
    submit_seconds: float = 0.0
    generate_seconds: float = 0.0
    poll_count: int = 0
    attempts: int = 1
    output: dict = field(default_factory=dict)   # длительность, разрешение, fps, вес
    cost: float | None = None

    def to_json(self, root: Path | None = None) -> dict:
        def rel(p: Path | None) -> str | None:
            if p is None:
                return None
            if root:
                try:
                    return p.relative_to(root).as_posix()
                except ValueError:
                    pass
            return p.as_posix()

        return {
            "cell_id": self.cell_id,
            "provider": self.provider,
            "status": self.status.value,
            "provider_job_id": self.provider_job_id,
            "output_path": rel(self.output_path),
            "share_path": rel(self.share_path),
            "preview_path": rel(self.preview_path),
            "error": self.error,
            "error_type": self.error_type,
            "error_payload": self.error_payload,
            "submit_seconds": round(self.submit_seconds, 2),
            "generate_seconds": round(self.generate_seconds, 2),
            "poll_count": self.poll_count,
            "attempts": self.attempts,
            "output": self.output,
            "cost": self.cost,
        }
