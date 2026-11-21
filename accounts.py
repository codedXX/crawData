"""本地账号配置：隔离登录目录与暂停记录，不读取账号密码或复制 Cookie。"""
import re
import uuid
from pathlib import Path

from store import Store


class AccountProfiles:
    def __init__(self, directory, profile, samples):
        self.directory = Path(directory)
        self.profile = Path(profile)
        self.samples = Path(samples)
        self.registry = Store(self.directory)
        saved = self.registry.read('accounts.json')
        self.entries = saved.get('accounts', [dict(id='default', name='原账号', ready=True)])
        self.active = saved.get('active', 'default')
        if not isinstance(self.entries, list) or not self.entries:
            raise ValueError('账号配置文件无效。')
        seen = set()
        for entry in self.entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get('id'), str)
                    or not re.fullmatch(r'default|[0-9a-f]{32}', entry['id'])
                    or entry['id'] in seen or not isinstance(entry.get('name'), str)
                    or not entry['name'].strip() or type(entry.get('ready')) is not bool):
                raise ValueError('账号配置文件无效。')
            seen.add(entry['id'])
        self.get(self.active)

    def get(self, ident):
        for entry in self.entries:
            if entry['id'] == ident:
                return entry
        raise ValueError('请选择已有的账号配置。')

    def save(self):
        self.registry.write('accounts.json', dict(active=self.active, accounts=self.entries))

    def add(self, name):
        if not isinstance(name, str) or len(name.strip()) > 40:
            raise ValueError('账号名称最多 40 字，可以留空自动命名，不需要填写手机号或密码。')
        name = name.strip()
        if not name:
            names = {entry['name'] for entry in self.entries}
            number = 1
            while f'新账号 {number}' in names:
                number += 1
            name = f'新账号 {number}'
        if any(entry['name'] == name for entry in self.entries):
            raise ValueError('账号名称已存在，请直接选择该配置。')
        entry = dict(id=uuid.uuid4().hex, name=name, ready=False)
        self.entries.append(entry)
        self.save()
        return entry

    def paths(self, ident):
        self.get(ident)
        if ident == 'default':
            return self.directory, self.profile, self.samples
        directory = self.directory / 'accounts' / ident
        return directory, self.profile.parent / 'accounts' / ident / 'boss', directory / 'examples'
