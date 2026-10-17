"""Shared filename rules: media extensions, episode markers and subtitle suffixes.

No network, filesystem, credential or executor API. Numbering is never inferred
from list order; ambiguous markers are returned as review reasons.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".m2ts", ".webm", ".mpg", ".mpeg", ".wmv", ".flv", ".iso"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"}
_SE = re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})[ ._-]*E(\d{1,3})(?!\d)", re.I)
_E = re.compile(r"(?<![A-Za-z0-9])EP?[ ._-]?(\d{1,3})(?!\d)", re.I)
_CN_NUMBER = r"[零〇一二两三四五六七八九十百\d]+"
_CN_EPISODE = re.compile(r"第\s*(" + _CN_NUMBER + r")\s*集")
_CN_SEASON = re.compile(r"第\s*(" + _CN_NUMBER + r")\s*季")
_SPECIAL = re.compile(r"(?<![A-Za-z0-9])(?:specials?|sp|ova|oad|nco[pe]|extras?|trailer|sample)(?:\d{1,3})?(?![A-Za-z0-9])|特别篇|特別篇|特典|番外|花絮|预告|預告", re.I)
_MULTI = re.compile(r"(?:E\d{1,3})[ ._-]*(?:E\d|[-~–—+&][ ._]*(?:E)?\d)|第\s*" + _CN_NUMBER + r"\s*[-~–—+&、]\s*" + _CN_NUMBER + r"\s*集", re.I)
_SUBTITLE_TOKENS = {
    "zh", "zho", "chi", "chs", "cht", "sc", "tc", "cn", "tw", "hans", "hant",
    "simplified", "traditional", "chinese", "en", "eng", "english", "ja", "jpn", "japanese",
    "ko", "kor", "korean", "fr", "fre", "fra", "french", "de", "ger", "deu", "es", "spa",
    "ru", "rus", "it", "ita", "pt", "por", "ar", "ara", "th", "tha", "vi", "vie", "id", "ind", "ms", "msa",
    "us", "gb", "jp", "kr", "br", "pt", "multi", "bilingual", "dual", "forced", "sdh", "cc", "default", "commentary",
    "简体", "繁体", "簡體", "繁體", "简中", "繁中", "簡中", "中英", "中文", "英文", "日语", "日語", "双语", "雙語",
}


def _integer(value: Any, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (str, int)) or not re.fullmatch(r"\d+", str(value)):
        return None
    number = int(value)
    return number if minimum <= number <= maximum else None


def _chinese_integer(value: str) -> int | None:
    if value.isascii() and value.isdigit():
        return int(value)
    digits = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    if "十" not in value and "百" not in value:
        return int("".join(str(digits[character]) for character in value))
    result, current = 0, 0
    for character in value:
        if character in digits:
            current = digits[character]
        elif character in {"十", "百"}:
            result += (current or 1) * (10 if character == "十" else 100)
            current = 0
        else:
            return None
    return result + current


def _component_error(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return "must_be_nonempty_text"
    if value in {".", ".."} or "/" in value or "\\" in value:
        return "path_components_not_allowed"
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        return "control_characters_not_allowed"
    if value.endswith((" ", ".")):
        return "trailing_space_or_dot_not_allowed"
    return None


def _episode_metadata(stem: str, item: dict, request: dict) -> tuple[int | None, int | None, list[str]]:
    reasons: list[str] = []
    pair_matches = list(_SE.finditer(stem))
    pairs = [(int(match[1]), int(match[2])) for match in pair_matches]
    cn_episodes = [_chinese_integer(match[1]) for match in _CN_EPISODE.finditer(stem)]
    cn_seasons = [_chinese_integer(match[1]) for match in _CN_SEASON.finditer(stem)]
    standalone = [int(match[1]) for match in _E.finditer(stem)
                  if not any(pair.start() <= match.start() < pair.end() for pair in pair_matches)]
    episodes = [pair[1] for pair in pairs] + cn_episodes + standalone
    seasons = [pair[0] for pair in pairs] + cn_seasons
    if _MULTI.search(stem) or len(episodes) > 1 or len(seasons) > 1:
        reasons.append("multiple_episode_or_season_markers_require_review")
    if _SPECIAL.search(stem) or 0 in seasons:
        reasons.append("special_episode_requires_review")
    parsed_episode = episodes[0] if len(episodes) == 1 else None
    parsed_season = seasons[0] if len(seasons) == 1 else None
    supplied_episode = _integer(item.get("episode"), 1, 999)
    supplied_season = _integer(item.get("season", request.get("season")), 1, 99)
    for field, supplied, parsed in (("episode", supplied_episode, parsed_episode), ("season", supplied_season, parsed_season)):
        if field in item and supplied is None:
            reasons.append("invalid_explicit_" + field)
        if supplied is not None and parsed is not None and supplied != parsed:
            reasons.append("explicit_" + field + "_disagrees_with_filename")
    episode = supplied_episode if supplied_episode is not None else parsed_episode
    season = supplied_season if supplied_season is not None else parsed_season
    if _integer(episode, 1, 999) is None:
        reasons.append("episode_unknown_no_sort_order_inference")
    if _integer(season, 1, 99) is None:
        reasons.append("season_unknown_no_default_season")
    return season, episode, reasons


def _subtitle_suffix(subtitle_stem: str, video_stem: str) -> str | None:
    if subtitle_stem == video_stem:
        return ""
    if not subtitle_stem.startswith(video_stem):
        return None
    suffix = subtitle_stem[len(video_stem):]
    if not suffix or suffix[0] not in "._-":
        return None
    tokens = re.split(r"[._-]", suffix[1:])
    if not tokens or any(not token or token.casefold() not in _SUBTITLE_TOKENS for token in tokens):
        return None
    return suffix
