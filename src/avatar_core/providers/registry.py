from __future__ import annotations

import logging

from ..config import Settings
from ..errors import ConfigError
from .base import AvatarProvider, Capabilities
from .h3 import H3Provider
from .kandinsky import KandinskyProvider

REGISTRY: dict[str, type[AvatarProvider]] = {
    H3Provider.name: H3Provider,
    KandinskyProvider.name: KandinskyProvider,
}


def build_provider(name: str, settings: Settings, logger: logging.Logger | None = None) -> AvatarProvider:
    if name not in REGISTRY:
        raise ConfigError(f"Неизвестный провайдер '{name}'. Доступны: {', '.join(REGISTRY)}")
    return REGISTRY[name](settings.provider(name), logger=logger)


def capabilities_of(name: str) -> Capabilities:
    if name not in REGISTRY:
        raise ConfigError(f"Неизвестный провайдер '{name}'")
    return REGISTRY[name].capabilities
