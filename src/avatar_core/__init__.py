"""avatar-core — общий слой для работы с моделями видео-аватаров.

Единственная часть проекта, которая когда-либо уедет на GitHub вместе с мини-аппом.
Ничего про эксперименты, матрицы прогонов и отчёты здесь быть не должно.
"""

from .basket import AUDIO, IMAGE, VIDEO, Asset, Basket
from .config import Settings, load_secrets, load_settings
from .models import JobResult, JobSpec, JobStatus
from .providers import AvatarProvider, Capabilities, build_provider, capabilities_of

__version__ = "0.1.0"

__all__ = [
    "AUDIO",
    "IMAGE",
    "VIDEO",
    "Asset",
    "Basket",
    "Settings",
    "load_secrets",
    "load_settings",
    "JobResult",
    "JobSpec",
    "JobStatus",
    "AvatarProvider",
    "Capabilities",
    "build_provider",
    "capabilities_of",
]
