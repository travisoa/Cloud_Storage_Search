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
        ("HDR", r"hdr(?!10)"), ("HLG", r"hlg"), ("SDR", r"sdr"))
_AUDIO_CODECS = (
    ("DDP", r"ddp|dd\+|e[ ._-]?ac[ ._-]?3|dolby[ ._-]*digital[ ._-]*plus"),
    ("AC3", r"ac[ ._-]?3|dd(?!\+)|dolby[ ._-]*digital(?![ ._-]*plus)"),
    ("TrueHD", r"(?:dolby[ ._-]*)?true[ ._-]*hd"),
    ("DTS-HD.MA", r"dts[ ._-]*hd[ ._-]*(?:ma|master[ ._-]*audio)"),
    ("DTS-HD.HRA", r"dts[ ._-]*hd[ ._-]*hra"),
    ("DTS-HD", r"dts[ ._-]*hd(?![ ._-]*(?:ma|hra|master))"),
    ("DTS-X", r"dts[ ._:-]*x"),
    ("DTS", r"dts(?![ ._:-]*(?:hd|x(?![0-9])))"),
    ("AAC", r"(?:he[ ._-]*)?aac"), ("FLAC", r"flac"),
    ("Opus", r"opus"), ("MP3", r"mp3"), ("LPCM", r"lpcm"), ("PCM", r"pcm"),
)
_CHANNELS = r"(?:[1-9]\.[01](?:\.[2-9])?|[1-9][0-9]?ch)"
# Release groups often glue labels together (4KHQHDR60FPS). A run is split only
# when it consists entirely of known label pieces, so words that merely start
# with one (4KIDS, UHDTV, HDRip) stay whole and remain unlabelled.
_GLUED_PIECES = (r"4320p|2160p|1440p|1080p|720p|576p|480p|8k|4k|uhd|hdr10\+|hdr10|hdr|dovi|dv|hlg|sdr"
                 r"|hevc|[hx]265|avc|[hx]264|av1|hq|hfr|\d{2,3}fps|10bit|8bit|web(?:dl|rip)?|bluray|remux")
_GLUED_RUN = re.compile(r"[a-z0-9+]+", re.I)


def _split_glued(text):
    def split(match):
        run = match.group(0)
        pieces = re.findall(_GLUED_PIECES, run, re.I)
        return " ".join(pieces) if len(pieces) > 1 and "".join(pieces).casefold() == run.casefold() else run
    return _GLUED_RUN.sub(split, text)


def _labels(value, patterns):
    normalized = _split_glued(unicodedata.normalize("NFKC", str(value)))
    return [{"value": canonical, "label": match.group(0)}
            for canonical, pattern in patterns
            for match in re.finditer(r"(?<![a-z0-9])(?:" + pattern + r")(?![a-z0-9])",
                                     normalized, re.I)]


def _audio_labels(value):
    """Keep codec/channel combinations from one explicit source context."""
    text = unicodedata.normalize("NFKC", str(value))
    labels = []
    spans = []
    for canonical, pattern in _AUDIO_CODECS:
        expression = (r"(?<![a-z0-9])(?:" + pattern + r")"
                      r"(?:[ ._-]*(" + _CHANNELS + r"))?(?![a-z0-9])")
        for match in re.finditer(expression, text, re.I):
            if any(start < match.end() and match.start() < end for start, end in spans):
                continue
            spans.append(match.span())
            channels = ("." if canonical[-1].isdigit() else "") + match[1].lower() if match[1] else ""
            labels.append({"value": canonical + channels,
                           "label": match.group(0)})
    atmos = _labels(text, (("Atmos", r"(?:dolby[ ._-]*)?atmos"),))
    # An Atmos label is not inferred from a codec, channel count or directory.
    if atmos and len({item["value"] for item in labels}) <= 1:
        if labels:
            for item in labels:
                item["value"] += ".Atmos"
                item["label"] += " + " + atmos[0]["label"]
        else:
            labels = atmos
    return labels


def _hdr_value(values):
    # A DV release can also explicitly name its HDR base layer. SDR together
    # with any HDR format, or unrelated HDR formats, remains a conflict.
    if values == {"SDR"}:
        return "SDR"
    if "SDR" not in values:
        for primary, compatible in (
                ("Dolby Vision", {"Dolby Vision", "HDR10+", "HDR10", "HDR"}),
                ("HDR10+", {"HDR10+", "HDR10", "HDR"}),
                ("HDR10", {"HDR10", "HDR"}), ("HLG", {"HLG", "HDR"})):
            if primary in values and values <= compatible:
                return primary
    return next(iter(values)) if len(values) == 1 else None


def _field(name, parent_path, patterns=None, *, labels_for=None, select=None):
    evidence = []
    contexts = [("filename", name), *[("parent_directory", part)
                for part in reversed(str(parent_path).split("/")) if part]]
    selected, basis = None, "unknown"
    for source, text in contexts:
        labels = labels_for(text) if labels_for else _labels(text, patterns)
        if not labels:
            continue
        evidence.extend({"source": source, "text": text, **label} for label in labels)
        if basis == "unknown":
            values = {label["value"] for label in labels}
            selected = select(values) if select else next(iter(values)) if len(values) == 1 else None
            basis = source if selected else "conflicting_" + source + "_labels"
    return selected, basis, evidence


def media_info(name, parent_path=""):
    """Return release-label information; never present it as stream inspection."""
    resolution, basis, evidence = _field(name, parent_path, _RESOLUTIONS)
    codec, codec_basis, codec_evidence = _field(name, parent_path, _CODECS)
    hdr, hdr_basis, hdr_evidence = _field(name, parent_path, _HDR, select=_hdr_value)
    audio, audio_basis, audio_evidence = _field(name, parent_path, labels_for=_audio_labels)
    return {"resolution": resolution, "basis": basis, "evidence": evidence,
            "codec": codec, "codec_basis": codec_basis, "codec_evidence": codec_evidence,
            "hdr": hdr, "hdr_basis": hdr_basis, "hdr_evidence": hdr_evidence,
            "audio": audio, "audio_basis": audio_basis, "audio_evidence": audio_evidence,
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


def _media_suffix(media, include_media_tags):
    tags = []
    if include_media_tags and media:
        if media.get("resolution") in {value for value, _ in _RESOLUTIONS}:
            tags.append(media["resolution"].upper())
        if media.get("hdr") in {value for value, _ in _HDR}:
            tags.append("DV" if media["hdr"] == "Dolby Vision" else media["hdr"])
        audio = media.get("audio")
        if isinstance(audio, str) and {item["value"] for item in _audio_labels(audio)} == {audio}:
            tags.append(audio)
    return "_" + "_".join(tags) if tags else ""


def episode_filename(prefix, season, episode, extension, media=None, include_media_tags=True):
    """Create an Infuse episode name whose title prefix equals its show folder."""
    if type(season) is not int or not 1 <= season <= 99 or type(episode) is not int or not 1 <= episode <= 999:
        raise ValueError("valid_season_and_episode_required")
    if not re.fullmatch(r"\.[a-zA-Z0-9]+", extension):
        raise ValueError("safe_media_extension_required")
    # Reuse the same validation even when an existing folder supplies the prefix.
    prefix = series_prefix(prefix, 2000, prefix)
    return f"{prefix}S{season:02d}E{episode:02d}{_media_suffix(media, include_media_tags)}{extension}"


def movie_filename(prefix, extension, media=None, include_media_tags=True):
    """Create ``片名 (年份)_2160P_DV_DDP5.1.mkv`` using the movie folder prefix."""
    tagged = episode_filename(prefix, 1, 1, extension, media, include_media_tags)
    prefix = series_prefix(prefix, 2000, prefix)
    return prefix + tagged[len(prefix) + len("S01E01"):]


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
