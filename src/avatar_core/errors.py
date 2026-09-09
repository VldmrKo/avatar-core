"""Исключения. Разные типы нужны, чтобы раннер знал, что ретраить, а что нет."""

from __future__ import annotations


class AvatarError(Exception):
    """Базовое."""


class ConfigError(AvatarError):
    """Нет ключа, кривой settings.yaml, отсутствует ffmpeg."""


class MediaError(AvatarError):
    """ffmpeg/ffprobe упал или отдал неожиданное."""


class BasketError(AvatarError):
    """Корзина не укладывается в возможности провайдера."""


class ProviderError(AvatarError):
    """Ошибка со стороны API."""

    retryable = False

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: int | None = None,
        payload: object = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.payload = payload

    def __str__(self) -> str:  # pragma: no cover - косметика
        head = super().__str__()
        bits = [b for b in (self.provider, self.status_code and f"HTTP {self.status_code}") if b]
        return f"[{' '.join(str(b) for b in bits)}] {head}" if bits else head


class AuthError(ProviderError):
    """401/403. Ретраить бессмысленно."""


class Rejected(ProviderError):
    """422 или отказ модерации. Запрос невалиден — ретраить бессмысленно."""


class RateLimited(ProviderError):
    """429. Ретраим с паузой."""

    retryable = True


class QueueFull(ProviderError):
    """Очередь воркера забита (503 и подобное). Ретраим с большой паузой."""

    retryable = True


class TransportError(ProviderError):
    """Сеть отвалилась, таймаут сокета, 5xx. Ретраим."""

    retryable = True


class JobFailed(ProviderError):
    """Задача принята, но завершилась ошибкой на стороне модели."""


class ResourceExhausted(JobFailed):
    """На бэкенде кончилась память GPU.

    Отдельно от JobFailed, потому что это не про наш запрос: тот же самый
    запрос через минуту пройдёт. Повторяем с большой паузой.
    """

    retryable = True


class PollTimeout(ProviderError):
    """Задача не досчиталась за отведённое время."""
