"""Per-series release choices, independent of write/overwrite authority."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import unicodedata

from .profiles import identity_key, private_json


RESOLUTIONS = ('2160p', '1080p', '720p', '480p')
CODECS = ('HEVC', 'H.264', 'AV1')
HDR = ('Dolby Vision', 'HDR10+', 'HDR10', 'HDR', 'HLG')
DEFAULT = {'resolution_order': ['2160p', '1080p', '720p'], 'allow_fallback': False,
           'preferred_codecs': [], 'require_codec': False, 'hdr': 'any',
           'preferred_keywords': [], 'min_size_mb': 0, 'max_size_gb': None,
           'archives': 'skip', 'existing': 'keep', 'prefer_size': 'any'}

# Library-wide presets chosen when a library is created. Sizes are per movie
# file or per episode file; they come from listings, not stream inspection.
PRESETS = {
    'quality': {'label': '品质优先', 'summary': '优先4K，无4K时用1080p；同档选体积更大的版本，偏好HDR，不设上限',
                'policy': {'resolution_order': ['2160p', '1080p'], 'allow_fallback': True, 'hdr': 'prefer',
                           'prefer_size': 'larger'},
                'size': {'movie': (3000, None), 'tv': (500, None)}},
    'balanced': {'label': '兼顾品质与存储', 'summary': '优先4K但限制体积（电影≤25GB、单集≤5GB），超限则用1080p；偏好HEVC与HDR，限额内选较大的版本',
                 'policy': {'resolution_order': ['2160p', '1080p'], 'allow_fallback': True,
                            'preferred_codecs': ['HEVC'], 'hdr': 'prefer', 'prefer_size': 'larger'},
                 'size': {'movie': (1500, 25), 'tv': (300, 5)}},
    'storage': {'label': '节省存储', 'summary': '优先1080p，其次720p，小体积4K也可；电影≤8GB、单集≤2GB；偏好HEVC/AV1，同档选体积更小的版本',
                'policy': {'resolution_order': ['1080p', '720p', '2160p'], 'allow_fallback': True,
                           'preferred_codecs': ['HEVC', 'AV1'], 'prefer_size': 'smaller'},
                'size': {'movie': (700, 8), 'tv': (150, 2)}},
}


def preset_choices():
    return [{'preset': key, 'label': value['label'], 'summary': value['summary']} for key, value in PRESETS.items()]


def preset_name(value):
    if value is not None and value not in PRESETS:
        raise ValueError('影库版本预设只能是 quality、balanced 或 storage')
    return value


def preset_policy(name, kind):
    preset = PRESETS[preset_name(name)]
    minimum, maximum = preset['size']['movie' if kind == 'movie' else 'tv']
    return normalize({**deepcopy(preset['policy']), 'min_size_mb': minimum, 'max_size_gb': maximum})


def normalize(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('版本策略包含不支持的字段')
    result = {**deepcopy(DEFAULT), **deepcopy(value)}
    for field, allowed in (('resolution_order', RESOLUTIONS), ('preferred_codecs', CODECS)):
        items = result[field]
        if not isinstance(items, list) or len(items) > len(allowed) or any(item not in allowed for item in items):
            raise ValueError('版本策略的分辨率或编码无效')
        result[field] = list(dict.fromkeys(items))
    if not result['resolution_order']:
        raise ValueError('至少需要一个目标分辨率')
    for field in ('allow_fallback', 'require_codec'):
        if type(result[field]) is not bool:
            raise ValueError('版本策略开关必须为布尔值')
    if result['require_codec'] and not result['preferred_codecs']:
        raise ValueError('要求指定编码时需同时提供编码列表')
    if result['hdr'] not in {'any', 'prefer', 'exclude', 'require', 'dolby-vision'}:
        raise ValueError('HDR策略无效')
    if result['prefer_size'] not in {'any', 'smaller', 'larger'}:
        raise ValueError('同档版本的体积偏好只能是 any、smaller 或 larger')
    if result['archives'] not in {'skip', 'manual'} or result['existing'] != 'keep':
        raise ValueError('压缩包仅支持跳过或显式处理；旧版本保持不动')
    keywords = result['preferred_keywords']
    if (not isinstance(keywords, list) or len(keywords) > 12
            or any(not isinstance(k, str) or not k.strip() or len(k) > 40
                   or re.search(r'https?://|cookie|token|密码|提取码', k, re.I)
                   or any(unicodedata.category(c) in {'Cc', 'Cs'} for c in k) for k in keywords)):
        raise ValueError('版本关键字须为不含凭据的简短发布标签')
    result['preferred_keywords'] = list(dict.fromkeys(k.strip() for k in keywords))
    for field in ('min_size_mb', 'max_size_gb'):
        number = result[field]
        if number is None and field == 'max_size_gb':
            continue
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) or number < 0 or number > 1000000):
            raise ValueError('版本文件大小限制无效')
    if result['max_size_gb'] is not None and result['max_size_gb'] * 1000 < result['min_size_mb']:
        raise ValueError('文件大小下限不能高于上限')
    return result


def assess(policy, info, *, size, name='', path=''):
    """Only release-label evidence is used; missing tags never prove SDR/codec."""
    selected = normalize(policy)
    reasons, order = [], selected['resolution_order']
    resolution = info.get('resolution')
    permitted = order if selected['allow_fallback'] else order[:1]
    if resolution not in permitted:
        reasons.append('resolution_unknown_or_not_allowed')
    if type(size) is not int or size <= 0:
        reasons.append('file_size_unverified')
    else:
        if size < selected['min_size_mb'] * 1000000:
            reasons.append('file_below_minimum_size')
        if selected['max_size_gb'] is not None and size > selected['max_size_gb'] * 1000000000:
            reasons.append('file_above_maximum_size')
    codec, hdr = info.get('codec'), info.get('hdr')
    codecs = selected['preferred_codecs']
    if selected['require_codec'] and codec not in codecs:
        reasons.append('required_codec_unverified_or_not_allowed')
    if selected['hdr'] == 'exclude' and hdr in HDR:
        reasons.append('hdr_label_excluded')
    if selected['hdr'] == 'require' and hdr not in HDR:
        reasons.append('required_hdr_unverified')
    if selected['hdr'] == 'dolby-vision' and hdr != 'Dolby Vision':
        reasons.append('required_dolby_vision_unverified')
    context = unicodedata.normalize('NFKC', name + ' ' + path).casefold()
    matched = [word for word in selected['preferred_keywords'] if unicodedata.normalize('NFKC', word).casefold() in context]
    # Resolution order dominates every combination of secondary preferences.
    score = (len(order) - order.index(resolution)) * 10000 if resolution in order else 0
    if codec in codecs:
        score += (len(codecs) - codecs.index(codec)) * 100
    if selected['hdr'] == 'prefer' and hdr in HDR:
        score += 50
    score += len(matched)
    # Size only separates otherwise equal versions; it stays below one point.
    if selected['prefer_size'] != 'any' and type(size) is int and 0 < size < 10 ** 15:
        fraction = size / 10 ** 15
        score += fraction if selected['prefer_size'] == 'larger' else 1 - fraction
    return {'allowed': not reasons, 'score': score, 'reasons': reasons,
            'matched_keywords': matched, 'basis': 'release_labels_only',
            'resolution': resolution, 'codec': codec, 'hdr': hdr,
            'hdr_absence_is_not_sdr_proof': hdr is None}


class VersionPolicyStore:
    """Callers hold workflow.run_lock for changes. Keys are stable show IDs."""
    def __init__(self, path):
        self.path = Path(path)

    def all(self):
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError('版本策略不能使用符号链接')
        if not self.path.exists():
            return {}
        if not self.path.is_file() or self.path.stat().st_mode & 0o077:
            raise ValueError('版本策略必须为私有普通文件')
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or set(data) != {'schema_version', 'series'}
                or data['schema_version'] != 1 or not isinstance(data['series'], dict)):
            raise ValueError('版本策略文件格式无效')
        for key, row in data['series'].items():
            if (not isinstance(row, dict) or set(row) != {'profile_key', 'title', 'metadata_source', 'series_id', 'year', 'policy', 'updated_at'}
                    or key != identity_key(row['metadata_source'], row['series_id'], row['year'])
                    or key != row['profile_key']
                    or normalize(row['policy']) != {'prefer_size': 'any', **row['policy']}):
                raise ValueError('版本策略作品绑定无效')
        return data['series']

    def get(self, request):
        key = identity_key(request['metadata_source'], request['series_id'], request['year'])
        return deepcopy(self.all().get(key))

    def set(self, request, policy):
        key = identity_key(request['metadata_source'], request['series_id'], request['year'])
        title = request['title']
        if (not isinstance(title, str) or not title.strip() or len(title) > 160
                or any(ord(c) < 32 for c in title) or re.search(r'https?://|cookie|token|密码|提取码', title, re.I)):
            raise ValueError('版本策略作品名无效')
        items = self.all()
        row = {'profile_key': key, 'title': title, 'metadata_source': request['metadata_source'],
               'series_id': str(int(str(request['series_id']))), 'year': request['year'],
               'policy': normalize(policy), 'updated_at': datetime.now(timezone.utc).isoformat()}
        items[key] = row
        private_json(self.path, {'schema_version': 1, 'series': items})
        return deepcopy(row)

    def reset(self, request):
        key = identity_key(request['metadata_source'], request['series_id'], request['year'])
        items = self.all()
        items.pop(key, None)
        private_json(self.path, {'schema_version': 1, 'series': items})


def attach(request, root, preset=None):
    """Refresh settings each cycle; a saved task cannot revive a reset policy.

    Precedence: a per-series policy, then an explicit quality choice for this
    task, then the library preset. An explicit confirmation sets
    ``library_preset_overridden`` so the preset no longer filters that task.
    """
    request = deepcopy(request)
    request.pop('version_policy', None)
    if request.pop('version_policy_key', None) is not None:
        request.pop('quality_preference', None)
    row = None
    if request.get('kind') != 'movie':
        row = VersionPolicyStore(Path(root) / 'runtime/version-policies.json').get(request)
    if row:
        request['version_policy'] = row['policy']
        request['quality_preference'] = list(row['policy']['resolution_order'])
        request['version_policy_key'] = row['profile_key']
    elif preset_name(preset) and not request.get('quality_preference') and not request.get('library_preset_overridden'):
        request['version_policy'] = preset_policy(preset, request.get('kind'))
        request['quality_preference'] = list(request['version_policy']['resolution_order'])
        request['version_policy_key'] = 'preset:' + preset
    return request


def override_preset(request):
    """An explicit user choice in this task replaces the library preset."""
    if str(request.get('version_policy_key') or '').startswith('preset:'):
        request.pop('version_policy_key', None)
        request.pop('version_policy', None)
        request.pop('quality_preference', None)
        request['library_preset_overridden'] = True
    return request
