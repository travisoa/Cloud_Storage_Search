"""Conservative naming from observed directory and release labels only.

These helpers do not probe streams. A resolution is a label in a filename or
the nearest labelled parent directory, and its provenance remains available.
"""

from __future__ import annotations

import re
import unicodedata


_RESOLUTIONS = (("4320p", r"4320p|8k"), ("2160p", r"2160p|4k|uhd"),
                ("1440p", r"1440p"), ("1080p", r"1080p"), ("720p", r"720p"),
                ("576p", r"576p"), ("480p", r"480p"))
_CODECS = (("HEVC", r"hevc|h[ ._-]?265|x265"), ("H.264", r"avc|h[ ._-]?264|x264"),
           ("AV1", r"av1"))
_HDR = (("Dolby Vision", r"dolby[ ._-]*vision|dovi|dv"),
        ("HDR10+", r"hdr10\+"), ("HDR10", r"hdr10(?!\+)"),
        ("HDR", r"hdr(?!10)"), ("HLG", r"hlg"))


def _labels(value, patterns):
    normalized = unicodedata.normalize("NFKC", str(value))
    return [{"value": canonical, "label": match.group(0)}
            for canonical, pattern in patterns
            for match in re.finditer(r"(?<![a-z0-9])(?:" + pattern + r")(?![a-z0-9])",
                                     normalized, re.I)]


def _field(name, parent_path, patterns):
    evidence = []
    contexts = [("filename", name), *[("parent_directory", part)
                for part in reversed(str(parent_path).split("/")) if part]]
    selected, basis = None, "unknown"
    for source, text in contexts:
        labels = _labels(text, patterns)
        if not labels:
            continue
        evidence.extend({"source": source, "text": text, **label} for label in labels)
        if basis == "unknown":
            values = {label["value"] for label in labels}
            selected = next(iter(values)) if len(values) == 1 else None
            basis = source if selected else "conflicting_" + source + "_labels"
    return selected, basis, evidence


def media_info(name, parent_path=""):
    """Return release-label information; never present it as stream inspection."""
    resolution, basis, evidence = _field(name, parent_path, _RESOLUTIONS)
    codec, codec_basis, codec_evidence = _field(name, parent_path, _CODECS)
    hdr, hdr_basis, hdr_evidence = _field(name, parent_path, _HDR)
    return {"resolution": resolution, "basis": basis, "evidence": evidence,
            "codec": codec, "codec_basis": codec_basis, "codec_evidence": codec_evidence,
            "hdr": hdr, "hdr_basis": hdr_basis, "hdr_evidence": hdr_evidence,
            "inspection": "release_labels_only"}


def series_prefix(title, year, observed_series_name=None):
    """Use the real show-directory name, never a Season directory name."""
    name = str(observed_series_name or "").strip()
    if not name or re.fullmatch(r"(?:season[ ._-]*\d+|第\s*\d+\s*季)", name, re.I):
        name = f"{title} ({int(year)})"
    if any(character in name for character in '/\\<>:"|?*') or any(
            unicodedata.category(character) in {"Cc", "Cs"} for character in name):
        raise ValueError("unsafe_series_naming_prefix")
    return name


def episode_filename(prefix, season, episode, extension, media=None, include_media_tags=True):
    """Create an Infuse episode name whose title prefix equals its show folder."""
    if type(season) is not int or not 1 <= season <= 99 or type(episode) is not int or not 1 <= episode <= 999:
        raise ValueError("valid_season_and_episode_required")
    if not re.fullmatch(r"\.[a-zA-Z0-9]+", extension):
        raise ValueError("safe_media_extension_required")
    # Reuse the same validation even when an existing folder supplies the prefix.
    prefix = series_prefix(prefix, 2000, prefix)
    tags = []
    if include_media_tags and media:
        for field, patterns in (("resolution", _RESOLUTIONS), ("codec", _CODECS), ("hdr", _HDR)):
            value = media.get(field)
            if value in {canonical for canonical, _ in patterns} and value not in tags:
                tags.append(value)
    suffix = " [" + " ".join(tags) + "]" if tags else ""
    return f"{prefix} S{season:02d}E{episode:02d}{suffix}{extension}"


def movie_filename(prefix, extension, media=None, include_media_tags=True):
    """Create ``片名 (年份) [2160p].mkv`` whose prefix equals the movie folder."""
    tagged = episode_filename(prefix, 1, 1, extension, media, include_media_tags)
    prefix = series_prefix(prefix, 2000, prefix)
    return prefix + tagged[len(prefix) + len(" S01E01"):]


def quality_score(info, preference):
    """Rank canonical label evidence using the configured preference order."""
    resolution = info.get("resolution")
    if not resolution:
        return 0
    for index, value in enumerate(preference):
        normalized = _labels(value, _RESOLUTIONS)
        canonical = {label["value"] for label in normalized}
        if resolution in canonical:
            return len(preference) - index
    return 0
