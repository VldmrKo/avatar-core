"""Конфигурация: секреты снаружи репозитория, настройки — в yaml.

Порядок чтения секретов:
    1. переменные окружения процесса
    2. файл AVATARS_ENV_FILE, если задан
    3. %USERPROFILE%\\.avatars\\secrets.env

Ключа нет — падаем сразу и с внятным текстом, а не на середине прогона.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .errors import ConfigError

DEFAULT_SECRETS = Path.home() / ".avatars" / "secrets.env"
_PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = _clean_value(value)
    return out


def _clean_value(raw: str) -> str:
    """Значение переменной без кавычек и без комментария в конце строки.

    `TOKEN=abc  # ключ от Пети` — вполне обычная запись, и без обрезки
    комментарий уезжает прямо в HTTP-заголовок.
    """
    value = raw.strip()
    if value[:1] in {'"', "'"} and value[-1:] == value[:1] and len(value) > 1:
        return value[1:-1]
    for marker in ("  #", "\t#", " #"):
        pos = value.find(marker)
        if pos != -1:
            value = value[:pos]
            break
    return value.strip().strip('"').strip("'")


def load_secrets(explicit: Path | None = None) -> dict[str, str]:
    """Секреты из файла, поверх — то, что уже есть в окружении процесса."""
    if explicit is not None:
        path = Path(explicit)
    else:
        # Осторожно: Path("") — это Path("."), и оно истинно. Пустую переменную
        # окружения надо отсекать явно, иначе получим попытку прочитать папку.
        from_env = os.environ.get("AVATARS_ENV_FILE", "").strip()
        path = Path(from_env) if from_env else DEFAULT_SECRETS
    values = _parse_env_file(path)
    # Переменные окружения перекрывают файл. На сервере секреты обычно
    # приходят из окружения юнита, а не из файла, поэтому префиксы мини-аппа
    # должны быть в этом списке наравне с провайдерскими.
    prefixes = ("H3_", "KANDINSKY_", "MAX_", "MINIAPP_")
    values.update({k: v for k, v in os.environ.items() if k in values or k.startswith(prefixes)})
    values["_secrets_path"] = str(path)
    return values


def _expand(node, secrets: dict[str, str]):
    """Подставляет ${VAR} внутри строк конфига."""
    if isinstance(node, str):
        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in secrets:
                raise ConfigError(
                    f"В конфиге есть ${{{name}}}, но такой переменной нет "
                    f"ни в окружении, ни в {secrets.get('_secrets_path')}"
                )
            return secrets[name]

        return _PLACEHOLDER.sub(sub, node)
    if isinstance(node, dict):
        return {k: _expand(v, secrets) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand(v, secrets) for v in node]
    return node


class ProviderSettings(BaseModel):
    enabled: bool = True
    base_url: str
    api_key: str = ""
    concurrency: int = 1
    connect_timeout_s: float = 15.0
    read_timeout_s: float = 180.0
    submit_retries: int = 4
    poll_interval_s: float = 5.0
    poll_timeout_s: float = 1800.0
    heartbeat_s: float = 30.0   # как часто писать «ещё ждём» в консоль
    job_retries: int = 0        # повторы задачи при временном сбое бэкенда
    retry_pause_s: float = 60.0 # пауза перед повтором, растёт с попытками
    cooldown_s: float = 0.0     # пауза после задачи: дать бэкенду отдать память
    max_request_mb: float = 60.0
    proxy: str = ""          # пусто = брать из HTTP(S)_PROXY окружения
    verify_tls: bool = True
    extra: dict = Field(default_factory=dict)


class AudioCanon(BaseModel):
    sample_rate: int = 24000
    channels: int = 1
    loudness_lufs: float = -18.0
    true_peak_db: float = -1.5
    trim_silence: bool = True
    silence_threshold_db: float = -35.0
    max_seconds: float = 14.0
    min_seconds: float = 2.0


class Settings(BaseModel):
    data_root: Path
    refs_root: Path
    runs_root: Path
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    audio: AudioCanon = Field(default_factory=AudioCanon)
    providers: dict[str, ProviderSettings]
    secrets_path: str = ""

    def provider(self, name: str) -> ProviderSettings:
        if name not in self.providers:
            raise ConfigError(f"Провайдер '{name}' не описан в settings.yaml")
        return self.providers[name]


def load_settings(path: str | Path, secrets: dict[str, str] | None = None) -> Settings:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Не найден конфиг {path}")
    secrets = secrets if secrets is not None else load_secrets()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = _expand(raw, secrets)
    settings = Settings(**raw)
    settings.secrets_path = secrets.get("_secrets_path", "")
    # Если ffmpeg не в PATH, но лежит распакованным рядом — подставляем абсолютный путь.
    from .media import resolve_tools

    settings.ffmpeg, settings.ffprobe = resolve_tools(settings.ffmpeg, settings.ffprobe)

    # HTTP-заголовки не бывают в кириллице. Ловим здесь, иначе получим
    # невнятный UnicodeEncodeError из недр httpx уже при создании клиента.
    for name, cfg in settings.providers.items():
        if cfg.api_key and not cfg.api_key.isascii():
            bad = "".join(dict.fromkeys(c for c in cfg.api_key if not c.isascii()))[:8]
            raise ConfigError(
                f"Ключ провайдера '{name}' содержит символы не из ASCII: {bad!r}. "
                f"Проверьте {settings.secrets_path}: в значении должен быть только сам токен. "
                "Частая причина — комментарий в той же строке или лишний текст после ключа."
            )
    return settings
