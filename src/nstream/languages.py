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

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str
    name: str  # native display name
    tokens: tuple[str, ...]  # release-name tokens (word-boundary matched, case-insensitive)
    flags: tuple[str, ...] = ()  # flag emoji Torrentio may prepend
    aliases: tuple[str, ...] = ()  # equivalent codes (ISO-639-1 + 639-2/B) for tag matching
    selectable: bool = True  # offered in the preference picker (False for `multi`)


LANGUAGES: tuple[Language, ...] = (
    Language("ita", "Italiano", ("ITA", "ITALIAN", "ITALIANO"), ("🇮🇹",), ("it",)),
    Language("eng", "English", ("ENG", "ENGLISH"), ("🇬🇧", "🇺🇸"), ("en",)),
    Language(
        "fra",
        "Français",
        ("FRA", "FRENCH", "TRUEFRENCH", "VFF", "VFQ", "VOSTFR"),
        ("🇫🇷",),
        ("fr", "fre"),
    ),  # noqa: E501
    Language(
        "spa", "Español", ("SPA", "ESP", "SPANISH", "CASTELLANO", "LATINO", "LAT"), ("🇪🇸",), ("es",)
    ),  # noqa: E501
    Language("deu", "Deutsch", ("GER", "GERMAN", "DEU"), ("🇩🇪",), ("de", "ger")),
    Language("rus", "Русский", ("RUS", "RUSSIAN"), ("🇷🇺",), ("ru",)),
    Language(
        "por", "Português", ("POR", "PORTUGUESE", "DUBLADO", "LEGENDADO"), ("🇵🇹", "🇧🇷"), ("pt",)
    ),  # noqa: E501
    Language("jpn", "日本語", ("JPN", "JAP", "JAPANESE"), ("🇯🇵",), ("ja", "jp")),
    Language("kor", "한국어", ("KOR", "KOREAN"), ("🇰🇷",), ("ko",)),
    Language("zho", "中文", ("ZHO", "CHI", "CHINESE", "MANDARIN", "CANTONESE"), ("🇨🇳",), ("zh",)),
    Language("nld", "Nederlands", ("NLD", "DUT", "DUTCH"), ("🇳🇱",), ("nl",)),
    Language("pol", "Polski", ("POL", "POLISH"), ("🇵🇱",), ("pl",)),
    Language("hin", "हिन्दी", ("HIN", "HINDI"), ("🇮🇳",), ("hi",)),
    Language("ara", "العربية", ("ARA", "ARABIC"), ("🇸🇦",), ("ar",)),
    Language("ces", "Čeština", ("CZE", "CZECH", "CZ"), ("🇨🇿",), ("cs", "cze")),
    Language("slk", "Slovenčina", ("SVK", "SLOVAK", "SK"), ("🇸🇰",), ("sk", "slo")),
    Language("hun", "Magyar", ("HUN", "HUNGARIAN"), ("🇭🇺",), ("hu",)),
    Language("ukr", "Українська", ("UKR", "UKRAINIAN"), ("🇺🇦",), ("uk",)),
    Language("tur", "Türkçe", ("TUR", "TURKISH"), ("🇹🇷",), ("tr",)),
    Language(
        "multi", "Multi", ("MULTI", "MULTILANG", "MULTI-LANG", "DUAL", "DUALAUDIO"), (), (), False
    ),
)

by_code: dict[str, Language] = {lang.code: lang for lang in LANGUAGES}

# Any tag form (canonical code, alias, release token, native/English name) → canonical code.
_NORMALIZE: dict[str, str] = {
    form.lower(): lang.code
    for lang in LANGUAGES
    for form in (lang.code, lang.name, *lang.aliases, *lang.tokens)
}


def name(code: str) -> str:
    """Display name for a code, falling back to the upper-cased code if unknown."""
    lang = by_code.get(code)
    return lang.name if lang else code.upper()


def normalize(tag: str) -> str | None:
    """Map any language tag (ISO-639-1/2, release token, or name) to the canonical code,
    or None if unknown. Used to match ffprobe track tags (often 2-letter, e.g. `it`/`en`)
    against the 3-letter codes stored in config."""
    return _NORMALIZE.get(tag.strip().lower()) if tag else None


def selectable() -> list[Language]:
    """Languages offered as audio/subtitle preferences (excludes the `multi` attribute)."""
    return [lang for lang in LANGUAGES if lang.selectable]


# Title-token matcher for ffprobe track titles. The release tokens already include the
# English language names (e.g. "ITALIAN", "ENGLISH"), so a title like "Italian [TrueHD]"
# matches case-insensitively. `multi` is excluded (not a target language). Word-boundary
# matched so "Italian" doesn't fire inside another word.
_TITLE_LANG_RE: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(r"(?<![A-Za-z])(?:" + "|".join(lang.tokens) + r")(?![A-Za-z])", re.I), lang.code)
    for lang in LANGUAGES
    if lang.selectable and lang.tokens
)


def track_lang(language_tag: str, title: str = "") -> str | None:
    """Canonical language code of a media track. Uses the ffprobe `language` tag first
    (via `normalize`, handling 2-letter forms like `it`/`en`); when that's missing or
    undefined (`und`), falls back to a language name in the track `title`
    (e.g. title='Italian [Dolby TrueHD Atmos]' → 'ita'). None if neither yields a language.

    This recovers the language of releases whose audio tracks are untagged for language
    but name it in the title — common in "Dual"/compact rips."""
    code = normalize(language_tag)
    if code:
        return code
    return next((c for pat, c in _TITLE_LANG_RE if pat.search(title)), None) if title else None
