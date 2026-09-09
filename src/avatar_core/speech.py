"""Оценка длительности речи по тексту.

Нужна, чтобы не просить у модели ролик заведомо длиннее реплики: лишние
секунды ей нечем занять, и она начинает повторять уже сказанное.
"""

from __future__ import annotations

import re

VOWELS_RU = "аеёиоуыэюяАЕЁИОУЫЭЮЯ"
VOWELS_EN = "aeiouyAEIOUY"

# Темп откалиброван по факту: реплика Андрея — 49 слогов, звучит около 10 с.
# Это ~5.5 слогов в секунду, обычный разговорный темп.
SYLLABLES_PER_SECOND = {"ru": 5.5, "en": 5.5}


def count_syllables(text: str, lang: str = "ru") -> int:
    """Слоги считаем по гласным — для оценки длительности этого достаточно."""
    vowels = VOWELS_RU + VOWELS_EN if lang == "ru" else VOWELS_EN
    return sum(1 for ch in text if ch in vowels)


def estimate_speech_seconds(
    text: str,
    lang: str = "ru",
    *,
    pause_factor: float = 1.08,
    sentence_pause_s: float = 0.30,
) -> float:
    """Сколько примерно займёт произнесение текста.

    Слоги делим на темп речи, добавляем запас на дыхание и по паузе
    на каждую точку — знаки препинания реально растягивают реплику.
    """
    clean = re.sub(r"\([^)]*\)", " ", text)          # ремарки в скобках вслух не идут
    clean = re.sub(r"<[^>]*>", " ", clean)           # теги разметки тоже
    syllables = count_syllables(clean, lang)
    if not syllables:
        return 0.0
    rate = SYLLABLES_PER_SECOND.get(lang, 5.5)
    sentences = len(re.findall(r"[.!?…]+", clean))
    return round(syllables / rate * pause_factor + sentences * sentence_pause_s, 1)


def fit_duration(text: str, lang: str = "ru", *, lo: int = 4, hi: int = 15,
                 headroom_s: float = 1.0) -> int:
    """Целое число секунд под реплику, зажатое в допустимый диапазон модели."""
    estimate = estimate_speech_seconds(text, lang) + headroom_s
    return max(lo, min(hi, round(estimate)))


def split_for_clips(text: str, lang: str = "ru", *, max_seconds: float = 12.0) -> list[str]:
    """Режет реплику на куски, каждый из которых укладывается в один клип.

    У H3 жёсткий потолок 15 секунд на генерацию, нативного продолжения нет.
    Длинную речь приходится собирать из нескольких клипов, и точки реза лучше
    выбирать самим — по границам предложений, где пауза естественна. Kandinsky
    режет сам, вслепую, и его швы видно именно поэтому.
    """
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    chunks: list[str] = []
    current = ""
    for part in parts:
        if not part:
            continue
        candidate = f"{current} {part}".strip()
        if current and estimate_speech_seconds(candidate, lang) > max_seconds:
            chunks.append(current)
            current = part
        else:
            current = candidate
    if current:
        chunks.append(current)

    # Предложение длиннее лимита само по себе — режем по запятым.
    out: list[str] = []
    for chunk in chunks:
        if estimate_speech_seconds(chunk, lang) <= max_seconds:
            out.append(chunk)
            continue
        buf = ""
        for piece in re.split(r"(?<=,)\s+", chunk):
            candidate = f"{buf} {piece}".strip()
            if buf and estimate_speech_seconds(candidate, lang) > max_seconds:
                out.append(buf)
                buf = piece
            else:
                buf = candidate
        if buf:
            out.append(buf)
    return out


def plan_clips(text: str, lang: str = "ru", *, max_seconds: float = 12.0,
               lo: int = 4, hi: int = 15) -> list[dict]:
    """Готовый план: куски реплики с длительностью под каждый клип."""
    return [
        {
            "index": i,
            "text": chunk,
            "estimate_s": estimate_speech_seconds(chunk, lang),
            "duration_seconds": fit_duration(chunk, lang, lo=lo, hi=hi),
        }
        for i, chunk in enumerate(split_for_clips(text, lang, max_seconds=max_seconds), 1)
    ]
