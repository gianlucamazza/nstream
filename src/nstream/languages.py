"""Single source of truth for the languages nstream understands.

Release-name tokens, flag emoji and display names used to live in three separate maps
(`quality._LANG_TOKENS`, `quality._FLAG_LANG`, `caster._LANG_NAMES`) that had to be kept in
sync by hand. They are all derived from `LANGUAGES` here instead, so adding a language is a
one-line change. Leaf module: imports nothing from nstream, so any module can use it without
an import cycle.

`code` is the ISO-639-style key stored in config (`audio_langs`/`subtitle_langs`) and matched
against parsed stream languages. `multi` is a release attribute (multi-audio), not a target
language, so it is not `selectable` in the preference picker.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str
    name: str  # native display name
    tokens: tuple[str, ...]  # release-name tokens (word-boundary matched, case-insensitive)
    flags: tuple[str, ...] = ()  # flag emoji Torrentio may prepend
    selectable: bool = True  # offered in the preference picker (False for `multi`)


LANGUAGES: tuple[Language, ...] = (
    Language("ita", "Italiano", ("ITA", "ITALIAN", "ITALIANO"), ("🇮🇹",)),
    Language("eng", "English", ("ENG", "ENGLISH"), ("🇬🇧", "🇺🇸")),
    Language("fra", "Français", ("FRA", "FRENCH", "TRUEFRENCH", "VFF", "VFQ", "VOSTFR"), ("🇫🇷",)),
    Language("spa", "Español", ("SPA", "ESP", "SPANISH", "CASTELLANO", "LATINO"), ("🇪🇸",)),
    Language("deu", "Deutsch", ("GER", "GERMAN", "DEU"), ("🇩🇪",)),
    Language("rus", "Русский", ("RUS", "RUSSIAN"), ("🇷🇺",)),
    Language("por", "Português", ("POR", "PORTUGUESE", "DUBLADO", "LEGENDADO"), ("🇵🇹", "🇧🇷")),
    Language("jpn", "日本語", ("JPN", "JAP", "JAPANESE"), ("🇯🇵",)),
    Language("kor", "한국어", ("KOR", "KOREAN"), ("🇰🇷",)),
    Language("zho", "中文", ("ZHO", "CHI", "CHINESE", "MANDARIN", "CANTONESE"), ("🇨🇳",)),
    Language("nld", "Nederlands", ("NLD", "DUT", "DUTCH"), ("🇳🇱",)),
    Language("pol", "Polski", ("POL", "POLISH"), ("🇵🇱",)),
    Language("hin", "हिन्दी", ("HIN", "HINDI"), ("🇮🇳",)),
    Language("ara", "العربية", ("ARA", "ARABIC"), ("🇸🇦",)),
    Language(
        "multi", "Multi", ("MULTI", "MULTILANG", "MULTI-LANG", "DUAL", "DUALAUDIO"), (), False
    ),
)

by_code: dict[str, Language] = {lang.code: lang for lang in LANGUAGES}


def name(code: str) -> str:
    """Display name for a code, falling back to the upper-cased code if unknown."""
    lang = by_code.get(code)
    return lang.name if lang else code.upper()


def selectable() -> list[Language]:
    """Languages offered as audio/subtitle preferences (excludes the `multi` attribute)."""
    return [lang for lang in LANGUAGES if lang.selectable]
