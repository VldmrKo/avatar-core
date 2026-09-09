"""Корзина — единая структура входа, одинаковая для всех провайдеров.

Провайдер объявляет свои возможности, корзина проверяется против них.
В режиме strict несовпадение = отказ запускать прогон, а не тихая деградация:
иначе сравнение перестаёт быть сравнением.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import wave
from dataclasses import dataclass, field
from pathlib import Path

from .media import MediaInfo, probe

IMAGE = "image"
AUDIO = "audio"
VIDEO = "video"

_MIME_FALLBACK = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".wav": "audio/wav", ".mp3": "audio/mpeg", ".mp4": "video/mp4"}


@dataclass
class Asset:
    path: Path
    kind: str
    _info: MediaInfo | None = field(default=None, repr=False)
    _sha: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if not self.path.exists():
            raise FileNotFoundError(f"Референс не найден: {self.path}")

    @property
    def mime(self) -> str:
        guess, _ = mimetypes.guess_type(self.path.name)
        return guess or _MIME_FALLBACK.get(self.path.suffix.lower(), "application/octet-stream")

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size

    @property
    def info(self) -> MediaInfo:
        """Метаданные ассета.

        Для WAV читаем заголовок стандартной библиотекой: канонический сэмпл —
        это всегда WAV, и благодаря быстрому пути валидация корзины и отправка
        запроса работают даже там, где ffmpeg поставить не дали. Всё остальное
        (видео, постобработка результата) без ffprobe действительно не обойдётся.
        """
        if self._info is None:
            self._info = self._wav_info() or probe(self.path)
        return self._info

    def _wav_info(self) -> MediaInfo | None:
        if self.kind != AUDIO or self.path.suffix.lower() != ".wav":
            return None
        try:
            with wave.open(str(self.path), "rb") as wf:
                rate = wf.getframerate()
                return MediaInfo(
                    duration_s=wf.getnframes() / rate if rate else 0.0,
                    audio_codec=f"pcm_s{wf.getsampwidth() * 8}le",
                    sample_rate=rate,
                    channels=wf.getnchannels(),
                    size_bytes=self.path.stat().st_size,
                )
        except (wave.Error, EOFError, OSError):
            return None   # сжатый или битый wav — пусть разбирается ffprobe

    @property
    def duration_s(self) -> float:
        return self.info.duration_s if self.kind in (AUDIO, VIDEO) else 0.0

    @property
    def sha256(self) -> str:
        if self._sha is None:
            h = hashlib.sha256()
            with self.path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            self._sha = h.hexdigest()
        return self._sha

    def b64(self) -> str:
        return base64.b64encode(self.path.read_bytes()).decode("ascii")

    def data_uri(self) -> str:
        return f"data:{self.mime};base64,{self.b64()}"

    def summary(self) -> dict:
        out = {
            "file": self.path.name,
            "path": str(self.path),
            "kind": self.kind,
            "mime": self.mime,
            "bytes": self.size_bytes,
            "sha256": self.sha256,
        }
        if self.kind in (AUDIO, VIDEO):
            i = self.info
            out |= {"duration_s": round(i.duration_s, 3)}
            if self.kind == AUDIO:
                out |= {"sample_rate": i.sample_rate, "channels": i.channels}
            else:
                out |= {"width": i.width, "height": i.height, "fps": i.fps}
        return out


@dataclass
class Basket:
    """Логический вход. Текст один и тот же для всех провайдеров — это принципиально."""

    text: str
    images: list[Asset] = field(default_factory=list)
    audios: list[Asset] = field(default_factory=list)
    videos: list[Asset] = field(default_factory=list)
    scene_ru: str = ""
    scene_en: str = ""
    meta: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "text": self.text,
            "scene_ru": self.scene_ru,
            "scene_en": self.scene_en,
            "images": [a.summary() for a in self.images],
            "audios": [a.summary() for a in self.audios],
            "videos": [a.summary() for a in self.videos],
            "meta": self.meta,
        }

    def payload_estimate_mb(self) -> float:
        """base64 раздувает на треть — считаем заранее, чтобы не ловить таймаут вслепую."""
        raw = sum(a.size_bytes for a in (*self.images, *self.audios, *self.videos))
        return round(raw * 4 / 3 / 1_048_576, 2)
