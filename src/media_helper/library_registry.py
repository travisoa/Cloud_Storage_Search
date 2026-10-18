"""Named drive references and initialized libraries; never stores credentials."""
from copy import deepcopy
import json
from pathlib import Path
import re

from .profiles import private_json
from .version_policy import preset_name


_FIELDS = {'library_id', 'drive_id', 'parent_id', 'library_root_id', 'name',
           'categories', 'init_job_key', 'test_only'}


def identifier(value, label='标识'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value):
        raise ValueError(label + '格式无效')
    return value


def local_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,47}', value):
        raise ValueError('本地名称须为小写字母开头的1–48位字母、数字、横线或下划线')
    return value


def drive_reference(drive_id, storage_id, *, boundary_root_id=None):
    local_id(drive_id)
    if type(storage_id) is not int or storage_id < 1:
        raise ValueError('需要明确的 AList 夸克挂载 ID')
    result = {'drive_id': drive_id, 'provider': 'quark',
              'credential_source': {'type': 'alist', 'storage_id': storage_id},
              'account_scope': f'quark:alist-storage:{storage_id}'}
    if boundary_root_id is not None:
        result['boundary_root_id'] = identifier(boundary_root_id, '可操作根目录')
    return result


def file_drive_reference(drive_id):
    """A drive whose Cookie lives in the project's private credential directory."""
    local_id(drive_id)
    if drive_id == 'current':
        raise ValueError('current 是默认网盘的保留名称')
    return {'drive_id': drive_id, 'provider': 'quark',
            'credential_source': {'type': 'file', 'name': f'drives/{drive_id}.json'},
            'account_scope': f'quark:file:{drive_id}'}


def expected_reference(drive):
    """Rebuild a stored reference from its own fields; any other field is rejected."""
    source = drive.get('credential_source') if isinstance(drive, dict) else None
    if not isinstance(source, dict):
        raise ValueError('网盘引用无效')
    if source.get('type') == 'file':
        return file_drive_reference(drive.get('drive_id'))
    return drive_reference(drive.get('drive_id'), source.get('storage_id'),
                           boundary_root_id=drive.get('boundary_root_id'))


class LibraryRegistry:
    """Callers hold the existing account lock for mutations."""
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError('影库注册文件不能是符号链接')
        if not self.path.exists():
            return {'schema_version': 1, 'drives': {}, 'libraries': {}}
        if not self.path.is_file() or self.path.stat().st_mode & 0o077:
            raise ValueError('影库注册文件必须是私有普通文件')
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or set(data) != {'schema_version', 'drives', 'libraries'}
                or data['schema_version'] != 1 or not isinstance(data['drives'], dict)
                or not isinstance(data['libraries'], dict)):
            raise ValueError('影库注册文件格式错误')
        for key, item in data['drives'].items():
            if not isinstance(item, dict) or item.get('drive_id') != key or item != expected_reference(item):
                raise ValueError('网盘引用无效，不接受凭据或账号替换')
        for key, item in data['libraries'].items():
            local_id(key)
            if (not isinstance(item, dict) or not _FIELDS <= set(item) <= _FIELDS | {'version_preset'}
                    or item.get('library_id') != key or type(item.get('test_only')) is not bool):
                raise ValueError('影库记录格式错误')
            self._validate_library(item, data)
        return data

    def _validate_library(self, item, data):
        if (not isinstance(item, dict) or not _FIELDS <= set(item) <= _FIELDS | {'version_preset'}
                or type(item.get('test_only')) is not bool):
            raise ValueError('影库记录字段无效')
        preset_name(item.get('version_preset'))
        local_id(item['library_id'])
        local_id(item['drive_id'])
        if item['drive_id'] != 'current' and item['drive_id'] not in data['drives']:
            raise ValueError('影库对应的网盘尚未注册')
        if (item['drive_id'] == 'current' or data['drives'][item['drive_id']].get('boundary_root_id')) and not item['test_only']:
            raise ValueError('当前已有影库的连接仅允许隔离测试，不能注册为新的转存目标')
        for field in ('parent_id', 'library_root_id'):
            identifier(item[field], field)
        name = item['name']
        if (not isinstance(name, str) or not name.strip() or len(name) > 160
                or any(c in name for c in '/\\') or any(ord(c) < 32 for c in name)):
            raise ValueError('影库名称无效')
        if not isinstance(item['init_job_key'], str) or not re.fullmatch(r'library-init:[0-9a-f]{64}', item['init_job_key']):
            raise ValueError('影库初始化记录无效')
        categories = item['categories']
        if not isinstance(categories, dict) or not categories or not {'tv', 'movie'} <= set(categories):
            raise ValueError('影库至少包含电影与电视剧分类')
        fids = set()
        for key, value in categories.items():
            local_id(key)
            if not isinstance(value, dict) or set(value) != {'folder_id', 'name'}:
                raise ValueError('影库分类无效')
            fid = identifier(value['folder_id'], '分类目录')
            if fid == item['library_root_id'] or fid in fids:
                raise ValueError('影库分类目录不能重复')
            fids.add(fid)
            if not isinstance(value['name'], str) or not value['name'].strip() or any(c in value['name'] for c in '/\\\r\n'):
                raise ValueError('影库分类名无效')

    def connect(self, drive):
        data = self.read()
        if drive != expected_reference(drive) or drive['drive_id'] == 'current':
            raise ValueError('网盘引用无效或使用了保留名称')
        previous = data['drives'].get(drive['drive_id'])
        if previous and previous != drive:
            raise ValueError('此网盘名称已绑定其他账号引用，不能覆盖')
        if any(key != drive['drive_id'] and value['credential_source'] == drive['credential_source']
               for key, value in data['drives'].items()):
            raise ValueError('该 AList 挂载已有网盘连接名称，不能重复注册')
        data['drives'][drive['drive_id']] = deepcopy(drive)
        private_json(self.path, data)
        return deepcopy(drive)

    def get_drive(self, drive_id, config):
        if drive_id == 'current':
            source = config.get('credential_source', {})
            if config.get('provider') != 'quark' or source.get('type') not in {'alist', 'file'}:
                raise ValueError('当前网盘需要 AList 夸克挂载或私有 Cookie 文件')
            if source['type'] == 'file':
                from .storage_connection import credential_source, account_scope
                return {'drive_id': 'current', 'provider': 'quark',
                        'credential_source': credential_source(config),
                        'account_scope': account_scope(config),
                        'boundary_root_id': identifier(config['library_root_id'], '可操作根目录')}
            return drive_reference('current', source.get('storage_id'), boundary_root_id=config['library_root_id'])
        local_id(drive_id)
        drive = self.read()['drives'].get(drive_id)
        if drive is None:
            raise ValueError('此网盘尚未连接，请先 library connect')
        return deepcopy(drive)

    def get_library(self, library_id):
        local_id(library_id)
        value = self.read()['libraries'].get(library_id)
        if value is None:
            raise ValueError('此影库尚未初始化完成')
        return deepcopy(value)

    def register(self, library):
        data = self.read()
        local_id(library['library_id'])
        self._validate_library(library, data)
        previous = data['libraries'].get(library['library_id'])
        if previous and previous != library:
            raise ValueError('此影库名称已绑定其他目录，不能覆盖')
        for key, value in data['libraries'].items():
            if key != library['library_id'] and value['drive_id'] == library['drive_id'] and value['library_root_id'] == library['library_root_id']:
                raise ValueError('此真实影库目录已有本地名称')
        data['libraries'][library['library_id']] = deepcopy(library)
        private_json(self.path, data)
        return deepcopy(library)

    def set_preset(self, library_id, preset):
        data = self.read()
        library = data['libraries'].get(local_id(library_id))
        if library is None:
            raise ValueError('此影库尚未初始化完成')
        if preset_name(preset) is None:
            library.pop('version_preset', None)
        else:
            library['version_preset'] = preset
        private_json(self.path, data)
        return deepcopy(library)

    def config_for(self, library_id, base):
        library = self.get_library(library_id)
        if library['test_only']:
            raise ValueError('隔离测试影库不能作为转存或追更目标')
        drive = self.get_drive(library['drive_id'], base)
        selected = {key: deepcopy(base[key]) for key in
                    ('mode', 'metadata_source', 'pansou_url', 'timezone', 'include_media_tags') if key in base}
        return {**selected, 'provider': drive['provider'], 'credential_source': drive['credential_source'],
                'library_root_id': library['library_root_id'], 'categories': deepcopy(library['categories']),
                'overwrite': False, 'delete': False, 'rename_existing_files': False, 'open_browser': False,
                **({'version_preset': library['version_preset']} if library.get('version_preset') else {})}
