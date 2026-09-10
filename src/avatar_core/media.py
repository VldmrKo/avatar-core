"""Работа с медиа через ffmpeg/ffprobe.

Здесь одна принципиальная вещь: аудио канонизируется ОДИН раз, и в обе модели
уходит байт в байт один и тот же файл. Иначе мы сравниваем не модели,
а разницу в конвертации.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import MediaError

ASPECT_PRESETS = ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]

log = logging.getLogger("avatar.media")

# Коды аварийного завершения Windows. Ловим их отдельно от обычных ошибок ffmpeg:
# это не «плохой файл», а упавший процесс, и такое имеет смысл повторить.
CRASH_CODES = {0xC0000005, 0xC0000409, 0xC000001D, 0xC0000374, 0xC0000094}
# То же самое на POSIX: убит сигналом (SIGSEGV, SIGBUS, SIGABRT) — напрямую или через оболочку.
POSIX_CRASH_CODES = {-11, -7, -6, -4, 139, 135, 134, 132}


def _is_crash(code: int) -> bool:
    return (code & 0xFFFFFFFF) in CRASH_CODES or code in POSIX_CRASH_CODES


@dataclass
class MediaInfo:
    duration_s: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    size_bytes: int = 0

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0


def _search_dirs() -> list[Path]:
    """Куда заглянуть, если ffmpeg не прописан в PATH.

    Портативная распаковка — самый частый сценарий на рабочих машинах, где
    установщики блокируются политикой. Заставлять человека править PATH ради
    двух exe-шников незачем.
    """
    dirs = [Path("C:/Avatars/tools/ffmpeg/bin"), Path("C:/ffmpeg/bin")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        winget = Path(local) / "Microsoft" / "WinGet"
        dirs.append(winget / "Links")
        dirs.extend(sorted(winget.glob("Packages/Gyan.FFmpeg*/*/bin")))
    return [d for d in dirs if d.is_dir()]


def find_tool(name: str) -> str | None:
    """Абсолютный путь к ffmpeg/ffprobe или None."""
    candidate = Path(name)
    if candidate.is_absolute():
        return str(candidate) if candidate.exists() else None
    found = shutil.which(name)
    if found:
        return found
    for directory in _search_dirs():
        for suffix in ("", ".exe"):
            path = directory / (name + suffix)
            if path.exists():
                return str(path)
    return None


def resolve_tools(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> tuple[str, str]:
    """Подставляет найденные абсолютные пути, если инструменты не в PATH."""
    return find_tool(ffmpeg) or ffmpeg, find_tool(ffprobe) or ffprobe


def ensure_tools(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> None:
    for tool in (ffmpeg, ffprobe):
        if find_tool(tool) is None:
            raise MediaError(
                f"Не найден {tool}. Проще всего: запустите get-ffmpeg.bat в папке lab — "
                "он положит портативный ffmpeg в C:\\Avatars\\tools\\ffmpeg, "
                "и править PATH не придётся."
            )


RUN_TIMEOUT_S = 300.0


def _run(cmd: list[str], retries: int = 2, timeout: float = RUN_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    """Запуск ffmpeg/ffprobe.

    stdin обязательно закрыт: ffmpeg читает консоль и на общем stdin несколько
    параллельных процессов дерутся за него — получается интерактивная подсказка
    «Enter command:» и зависший прогон. То же самое делает флаг -nostdin,
    но глушим с обеих сторон, чтобы не зависеть от того, где команда собрана.

    Аварийное завершение процесса (0xC0000005 и родня) повторяем: на машинах
    с антивирусом, сканирующим свежесозданные файлы на лету, такое случается
    время от времени и со второй попытки обычно проходит. Обычные ошибки
    ffmpeg — битый файл, плохие аргументы — не повторяем, они детерминированы.
    """
    cmd = [find_tool(cmd[0]) or cmd[0], *cmd[1:]]
    for attempt in range(retries + 1):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # Без таймаута зависший ffmpeg вешает весь прогон: постобработка
            # идёт под общим замком, и следом встают все остальные ячейки.
            raise MediaError(
                f"{Path(cmd[0]).name} не ответил за {timeout:.0f} с и был снят. "
                f"Команда: {' '.join(str(c) for c in cmd[:8])}…"
            ) from exc
        except FileNotFoundError as exc:
            raise MediaError(
                f"Не найден {cmd[0]}. Запустите get-ffmpeg.bat в папке lab — он положит "
                "портативный ffmpeg в C:\\Avatars\\tools\\ffmpeg, PATH править не нужно."
            ) from exc
        if proc.returncode == 0:
            return proc
        crashed = _is_crash(proc.returncode)
        if crashed and attempt < retries:
            log.warning(
                "%s аварийно завершился (попытка %d из %d), повторяю",
                Path(cmd[0]).name, attempt + 1, retries + 1,
            )
            time.sleep(0.6 * (attempt + 1))
            continue
        raise MediaError(_failure_text(cmd, proc.returncode, proc.stderr))
    raise MediaError(_failure_text(cmd, -1, None))  # недостижимо, но mypy спокойнее


def _failure_text(cmd: list[str], code: int, stderr: str | None) -> str:
    name = Path(cmd[0]).name
    tail = " | ".join((stderr or "").strip().splitlines()[-6:])
    # Windows отдаёт коды падений как большие беззнаковые числа.
    signed = code - (1 << 32) if code > (1 << 31) else code
    known = {
        0xC0000005: "нарушение доступа (0xC0000005), не помогли и повторы — почти наверняка "
                    "антивирус проверяет ffmpeg.exe на каждом запуске. Добавьте "
                    "C:\\Avatars\\tools\\ffmpeg в исключения, либо перекачайте сборку: "
                    "удалите эту папку и запустите get-ffmpeg.bat заново",
        0xC0000409: "переполнение стека (0xC0000409) — сборка ffmpeg повреждена",
    }
    hint = known.get(code & 0xFFFFFFFF, "")
    parts = [f"{name} упал, код {signed}"]
    if hint:
        parts.append(hint)
    if tail:
        parts.append(tail)
    return ": ".join(parts)


def probe(path: str | Path, ffprobe: str = "ffprobe") -> MediaInfo:
    path = Path(path)
    proc = _run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)])
    data = json.loads(proc.stdout)
    info = MediaInfo(size_bytes=path.stat().st_size)
    try:
        info.duration_s = float(data.get("format", {}).get("duration", 0.0))
    except (TypeError, ValueError):
        info.duration_s = 0.0
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not info.width:
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
            info.video_codec = stream.get("codec_name", "")
            info.fps = _parse_fps(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/0")
        elif stream.get("codec_type") == "audio" and not info.audio_codec:
            info.audio_codec = stream.get("codec_name", "")
            info.sample_rate = int(stream.get("sample_rate") or 0)
            info.channels = int(stream.get("channels") or 0)
    return info


def _parse_fps(value: str) -> float:
    try:
        num, _, den = value.partition("/")
        den_f = float(den or 1)
        return round(float(num) / den_f, 3) if den_f else 0.0
    except (TypeError, ValueError):
        return 0.0


def detect_speech_window(
    path: str | Path, threshold_db: float = -35.0, min_silence_s: float = 0.25, ffmpeg: str = "ffmpeg"
) -> tuple[float, float]:
    """Возвращает (начало речи, конец речи) по silencedetect. Не находит — отдаёт весь файл."""
    info = probe(path)
    proc = subprocess.run(
        [find_tool(ffmpeg) or ffmpeg, "-nostdin", "-hide_banner", "-v", "info", "-i", str(path),
         "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence_s}", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=RUN_TIMEOUT_S,
    )
    log = proc.stderr or ""
    starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    begin = 0.0
    if ends and (not starts or starts[0] <= 0.05):
        begin = ends[0]
    # Хвостовая тишина — та, что доходит до конца файла. Собираем участки
    # тишины парами и смотрим только на последний: если он упирается в конец,
    # это хвост, и речь кончается там, где он начался. Иначе хвоста нет.
    #
    # Раньше здесь брался ПЕРВЫЙ silence_start в последних 2.5 с, и любая
    # пауза внутри записи обрезала всё, что после неё. На записи
    # «Вот это круто! <пауза> Вот это круто!» отваливался второй дубль,
    # а у пользователя мини-аппа, который говорит с паузами, — половина
    # образца голоса. ffmpeg при этом иногда дописывает silence_end на EOF,
    # а иногда нет, так что считать по длине списков нельзя.
    finish = info.duration_s
    regions = list(zip(starts, ends + [info.duration_s] * (len(starts) - len(ends))))
    if regions:
        last_start, last_end = regions[-1]
        if last_end >= info.duration_s - 0.05 and last_start > begin:
            finish = last_start
    if finish - begin < 0.5:
        return 0.0, info.duration_s
    return begin, finish


def _loudness_segments(
    path: str | Path,
    *,
    min_segment_s: float = 0.25,
    min_gap_s: float = 0.12,
    below_peak_db: float = 30.0,
    ffmpeg: str = "ffmpeg",
) -> list[tuple[float, float]] | None:
    """Запасной способ разметки речи: громкость относительно пика.

    Нужен там, где нет webrtcvad — под свежий Python колёс для него не
    собирают, а собирать из исходников на рабочей машине никто не будет.
    Прежний откат (detect_speech_window) отдавал ОДИН отрезок «от первого
    звука до последнего» и поэтому не видел пауз внутри ролика вообще:
    в отчёте это выглядело как «речь 100% времени» на ролике, где на слух
    посередине явная пауза.

    Порог берём не абсолютный, а на 30 дБ ниже пика: громкость генераций
    гуляет, и −35 дБ на тихом ролике режет речь, а на громком не видит
    тишину. Отдаём None, если numpy нет, — тогда зовущий откатится дальше.
    """
    try:
        import numpy as np  # noqa: PLC0415 — необязательная зависимость
    except ImportError:
        return None
    rate, hop = 16000, 320  # 20 мс
    proc = subprocess.run(
        [find_tool(ffmpeg) or ffmpeg, "-nostdin", "-v", "error", "-i", str(path),
         "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        capture_output=True, stdin=subprocess.DEVNULL, timeout=RUN_TIMEOUT_S,
    )
    if not proc.stdout:
        return []
    a = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    frames = a[: len(a) // hop * hop].reshape(-1, hop)
    if not len(frames):
        return []
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
    loud = db > db.max() - below_peak_db

    # Короткие провалы внутри фразы — это смычки согласных, а не паузы.
    gap = max(1, int(min_gap_s / (hop / rate)))
    idx = np.flatnonzero(loud)
    if not len(idx):
        return []
    spans: list[list[int]] = [[int(idx[0]), int(idx[0])]]
    for i in idx[1:]:
        if i - spans[-1][1] <= gap:
            spans[-1][1] = int(i)
        else:
            spans.append([int(i), int(i)])

    step = hop / rate
    out = [(round(s * step, 2), round((e + 1) * step, 2)) for s, e in spans]
    return [(a, b) for a, b in out if b - a >= min_segment_s]


def speech_segments(
    path: str | Path,
    *,
    aggressiveness: int = 2,
    frame_ms: int = 30,
    min_segment_s: float = 0.25,
    threshold_db: float = -35.0,
    ffmpeg: str = "ffmpeg",
) -> tuple[list[tuple[float, float]], str]:
    """Где в ролике звучит речь. Возвращает (сегменты, каким способом посчитано).

    Главная метрика этого стенда: модель заполняет речью отведённое время,
    и «сколько процентов ролика человек говорит» отвечает на это числом,
    а не пересматриванием каждого ролика вручную.

    Считаем через webrtcvad, если он поставлен: он отличает голос от шума
    движения. Если его нет — откатываемся на silencedetect, который меряет
    просто «есть звук / нет звука», и это честно помечается в способе.
    """
    tool = find_tool(ffmpeg) or ffmpeg
    info = probe(path)
    try:
        import webrtcvad  # noqa: PLC0415 — необязательная зависимость
    except ImportError:
        segments = _loudness_segments(path, min_segment_s=min_segment_s, ffmpeg=tool)
        if segments is not None:
            return segments, "громкость (numpy)"
        begin, finish = detect_speech_window(path, threshold_db=threshold_db, ffmpeg=ffmpeg)
        segments = [(begin, finish)] if finish - begin >= min_segment_s else []
        return segments, "звук (silencedetect)"

    rate = 16000
    proc = subprocess.run(
        [tool, "-nostdin", "-v", "error", "-i", str(path),
         "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        capture_output=True, stdin=subprocess.DEVNULL, timeout=RUN_TIMEOUT_S,
    )
    pcm = proc.stdout
    if not pcm:
        return [], "речь (webrtcvad)"

    vad = webrtcvad.Vad(aggressiveness)
    step = int(rate * frame_ms / 1000) * 2
    flags = [vad.is_speech(pcm[i:i + step], rate) for i in range(0, len(pcm) - step + 1, step)]
    # Сглаживание: одиночный кадр — это щелчок или вдох, а не речь.
    smooth = [sum(flags[max(0, i - 3):i + 4]) * 2 > len(flags[max(0, i - 3):i + 4])
              for i in range(len(flags))]

    segments: list[tuple[float, float]] = []
    start: float | None = None
    for i, flag in enumerate(smooth):
        at = i * frame_ms / 1000
        if flag and start is None:
            start = at
        elif not flag and start is not None:
            if at - start >= min_segment_s:
                segments.append((round(start, 2), round(at, 2)))
            start = None
    if start is not None:
        segments.append((round(start, 2), round(info.duration_s, 2)))
    return segments, "речь (webrtcvad)"


def flatness(
    path: str | Path,
    *,
    at_seconds: tuple[float, ...] = (0.5, 0.5, 0.5),
    box: int = 512,
    ffmpeg: str = "ffmpeg",
) -> float | None:
    """Насколько кадр нарисован, а не снят. Доля плоских заливок, 0…1.

    У модели сильный уклон в фотореализм: подаёшь ей плоскую иллюстрацию,
    а она достраивает кожу, поры и блики. На глаз это спорят словами
    «ну как-то не так», числом — не спорят.

    Меряем локальный разброс яркости в окне 3×3. У заливки он около нуля,
    у фотографии — нет. На наших образцах: рисунок 67%, фотография 24%,
    генерация по рисунку 37–48%, то есть больше половины пути к фото.

    Требует numpy. Нет его — возвращаем None, и метрика просто не покажется.
    """
    try:
        import numpy as np
        from numpy.lib.stride_tricks import sliding_window_view
        from PIL import Image
    except ImportError:
        # numpy или Pillow не поставлены — метрика просто не считается.
        return None

    tool = find_tool(ffmpeg) or ffmpeg
    # Именно find_tool, а не замена подстроки: путь вида
    # C:\Avatars\tools\ffmpeg\bin\ffmpeg.exe превращается заменой
    # в несуществующий C:\Avatars\tools\ffprobe\bin\ffprobe.exe.
    info = probe(path, ffprobe=find_tool("ffprobe") or "ffprobe")
    # Несколько кадров по ролику: стиль может уплывать к середине.
    moments = [info.duration_s * share for share in (0.1, 0.5, 0.9)] if info.duration_s else [0.0]

    scores: list[float] = []
    for moment in moments:
        proc = subprocess.run(
            [tool, "-nostdin", "-v", "error", "-ss", f"{moment:.2f}", "-i", str(path),
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=60,
        )
        if not proc.stdout:
            continue
        import io

        image = Image.open(io.BytesIO(proc.stdout)).convert("RGB")
        image.thumbnail((box, box), Image.LANCZOS)
        grey = np.asarray(image, dtype=np.float32).mean(axis=2)
        if min(grey.shape) < 3:
            continue
        window = sliding_window_view(grey, (3, 3))
        spread = window.reshape(*window.shape[:2], 9).std(axis=2)
        scores.append(float((spread < 1.5).mean()))
    return round(sum(scores) / len(scores), 3) if scores else None


def drift(
    path: str | Path,
    *,
    box: int = 256,
    ffmpeg: str = "ffmpeg",
) -> float | None:
    """Насколько кадр уезжает по ходу ролика. 0 — не шелохнулся, 1 — всё чужое.

    Нужно там, где вход уже готовая картинка и её надо УДЕРЖАТЬ: стикер,
    рисованный портрет, вырубленная наклейка. Модели любят «дооживить»
    кадр — наехать камерой, опустить руку, перекомпоновать сцену, — и на
    глаз это спор о вкусах: одному «почти не уехало», другому «да он же
    палец опустил». Числом не спорят.

    Меряем ВНУТРИ ролика: каждый кадр против первого. Сравнивать с исходной
    картинкой заманчиво, но нечестно — у разных моделей разный формат кадра,
    и половина «дрейфа» окажется разницей пропорций, а не движением. Внутри
    одного ролика формат постоянен, и число получается сравнимое.

    Считаем по яркости в маленьком окне: нам важно смещение композиции,
    а не шум кодека и не оттенок.
    """
    try:
        import io  # noqa: PLC0415

        import numpy as np  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return None

    tool = find_tool(ffmpeg) or ffmpeg
    info = probe(path, ffprobe=find_tool("ffprobe") or "ffprobe")
    if not info.duration_s:
        return None
    moments = [info.duration_s * share for share in (0.02, 0.25, 0.5, 0.75, 0.97)]

    frames: list[object] = []
    for moment in moments:
        proc = subprocess.run(
            [tool, "-nostdin", "-v", "error", "-ss", f"{moment:.2f}", "-i", str(path),
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=60,
        )
        if not proc.stdout:
            continue
        image = Image.open(io.BytesIO(proc.stdout)).convert("L").resize(
            (box, box), Image.LANCZOS)
        frames.append(np.asarray(image, dtype=np.float32) / 255.0)

    if len(frames) < 2:
        return None
    first = frames[0]
    # Максимум, а не среднее: важно самое сильное расхождение за ролик.
    # Кадр может уехать к концу и вернуться — для «удержал ли образ»
    # это всё равно уехал.
    return round(max(float(np.abs(other - first).mean()) for other in frames[1:]), 3)


def canon_audio(
    src: str | Path,
    dst: str | Path,
    *,
    sample_rate: int = 24000,
    channels: int = 1,
    loudness_lufs: float = -18.0,
    true_peak_db: float = -1.5,
    trim_silence: bool = True,
    silence_threshold_db: float = -35.0,
    max_seconds: float = 14.0,
    ffmpeg: str = "ffmpeg",
) -> MediaInfo:
    """WAV PCM 16-bit mono с нормализованной громкостью и обрезанной тишиной."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    begin, finish = (0.0, probe(src).duration_s)
    if trim_silence:
        begin, finish = detect_speech_window(src, silence_threshold_db, ffmpeg=ffmpeg)
    begin = max(0.0, begin - 0.07)
    finish = min(finish + 0.10, begin + max_seconds)

    cmd = [
        ffmpeg, "-nostdin", "-hide_banner", "-y", "-v", "error",
        "-ss", f"{begin:.3f}", "-to", f"{finish:.3f}", "-i", str(src),
        "-vn",
        "-af", f"loudnorm=I={loudness_lufs}:TP={true_peak_db}:LRA=11,afade=t=in:st=0:d=0.05",
        "-c:a", "pcm_s16le", "-ar", str(sample_rate), "-ac", str(channels),
        str(dst),
    ]
    _run(cmd)
    return probe(dst)


def to_share_mp4(src: str | Path, dst: str | Path, ffmpeg: str = "ffmpeg") -> MediaInfo:
    """H.264 yuv420p + AAC + faststart. Такое едят все мессенджеры."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-nostdin", "-hide_banner", "-y", "-v", "error", "-i", str(src),
        "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-crf", "20",
        "-preset", "medium",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-movflags", "+faststart",
        str(dst),
    ]
    _run(cmd)
    return probe(dst)


def thumbnail(src: str | Path, dst: str | Path, at_s: float = 1.0, ffmpeg: str = "ffmpeg") -> Path:
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run([ffmpeg, "-nostdin", "-hide_banner", "-y", "-v", "error", "-ss", f"{at_s:.2f}", "-i", str(src),
          "-frames:v", "1", "-q:v", "3", str(dst)])
    return dst


def detect_cuts(
    path: str | Path, threshold: float = 0.12, ffmpeg: str = "ffmpeg"
) -> list[dict]:
    """Резкие смены кадра внутри ролика.

    Говорящая голова на статичной камере меняться скачком не должна. Если
    скачок есть — это шов между кусками, которыми модель собирала ролик.
    Дефект заметный, но глазами его ловить по десяти роликам неудобно,
    поэтому меряем: ffmpeg считает scene score для каждого кадра, а мы
    оставляем те, что выше порога.
    """
    proc = subprocess.run(
        [find_tool(ffmpeg) or ffmpeg, "-nostdin", "-hide_banner", "-v", "info", "-i", str(path),
         "-vf", f"select='gt(scene,{threshold})',metadata=print", "-an", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=RUN_TIMEOUT_S,
    )
    log = proc.stderr or ""
    cuts: list[dict] = []
    pending: float | None = None
    for line in log.splitlines():
        stamp = re.search(r"pts_time:([0-9.]+)", line)
        if stamp:
            pending = float(stamp.group(1))
            continue
        score = re.search(r"lavfi\.scene_score=([0-9.]+)", line)
        if score and pending is not None:
            cuts.append({"at_s": round(pending, 2), "score": round(float(score.group(1)), 3)})
            pending = None
    # Первый кадр всегда «смена сцены» — это не шов.
    return [c for c in cuts if c["at_s"] > 0.3]


def last_frame(src: str | Path, dst: str | Path, ffmpeg: str = "ffmpeg") -> Path:
    """Последний кадр клипа — стартовая точка для следующего звена цепочки."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run([ffmpeg, "-nostdin", "-hide_banner", "-y", "-v", "error", "-sseof", "-0.1",
          "-i", str(src), "-frames:v", "1", "-q:v", "2", str(dst)])
    return dst


def concat_clips(
    parts: list[str | Path], dst: str | Path, *, audio_crossfade_s: float = 0.12,
    ffmpeg: str = "ffmpeg",
) -> MediaInfo:
    """Склейка клипов в один ролик.

    Видео стыкуется встык — плавный переход между разными генерациями всё
    равно не спасёт, а вот звук на стыке щёлкает, поэтому его сшиваем
    коротким кроссфейдом.
    """
    parts = [Path(p) for p in parts]
    if not parts:
        raise MediaError("Нечего склеивать: список клипов пуст")
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if len(parts) == 1:
        shutil.copy2(parts[0], dst)
        return probe(dst)

    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-y", "-v", "error"]
    for part in parts:
        cmd += ["-i", str(part)]

    n = len(parts)
    video_chain = "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
    if audio_crossfade_s > 0:
        steps, prev = [], "[0:a]"
        for i in range(1, n):
            label = f"[a{i}]" if i < n - 1 else "[a]"
            steps.append(f"{prev}[{i}:a]acrossfade=d={audio_crossfade_s}:c1=tri:c2=tri{label}")
            prev = label
        audio_chain = ";".join(steps)
    else:
        audio_chain = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[a]"

    cmd += ["-filter_complex", f"{video_chain};{audio_chain}", "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)]
    _run(cmd)
    return probe(dst)


def nearest_aspect(width: int, height: int, presets: list[str] | None = None) -> tuple[str, float]:
    """Ближайший из пресетов H3 к реальному соотношению. Возвращает (пресет, отклонение)."""
    presets = presets or ASPECT_PRESETS
    if not height:
        return "16:9", math.inf
    ratio = width / height
    scored = []
    for p in presets:
        a, b = (float(x) for x in p.split(":"))
        scored.append((abs(a / b - ratio), p))
    delta, best = min(scored)
    return best, round(delta, 4)
