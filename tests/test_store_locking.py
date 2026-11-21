"""本地状态读取与原子替换的 Windows 占用回归，不访问招聘网站。"""
import errno
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from guard import Guard
from store import Store, write_json


def sharing_error(code):
    error = PermissionError(errno.EACCES, 'file is busy')
    error.winerror = code
    return error


class JsonLockingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.path = Path(self.temp.name) / 'quota.json'
        self.old = dict(requests=7, blocked_until=2000000000, needs_verify=True)
        write_json(self.path, self.old)

    def test_transient_windows_errors_retry_same_atomic_write(self):
        replace = os.replace
        for code in (5, 32, 33):
            calls = []

            def flaky(source, target):
                calls.append((source, target))
                if len(calls) < 3:
                    raise sharing_error(code)
                replace(source, target)

            with self.subTest(code=code), patch('store.sys.platform', 'win32'), \
                    patch('store.os.replace', side_effect=flaky), patch('store.time.sleep') as sleep:
                write_json(self.path, dict(self.old, requests=8))
                self.assertEqual(len(calls), 3)
                self.assertEqual(len(set(calls)), 1)
                self.assertEqual(sleep.call_count, 2)
                self.assertEqual(self.store.read('quota.json'), dict(self.old, requests=8))
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_persistent_denial_preserves_pause_and_reports_actionable_error(self):
        with patch('store.sys.platform', 'win32'), \
                patch('store.os.replace', side_effect=sharing_error(5)) as replace, \
                patch('store.time.sleep') as sleep:
            with self.assertRaisesRegex(PermissionError, '无法保存本地状态.*quota.json'):
                write_json(self.path, dict(self.old, requests=8))
        self.assertEqual(replace.call_count, 7)
        self.assertEqual(sleep.call_count, 6)
        self.assertEqual(self.store.read('quota.json'), self.old)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_unrelated_disk_failure_is_not_retried(self):
        with patch('store.os.replace', side_effect=OSError(errno.ENOSPC, 'disk full')) as replace, \
                patch('store.time.sleep') as sleep:
            with self.assertRaises(OSError):
                write_json(self.path, {})
        replace.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(self.store.read('quota.json'), self.old)

    def test_polling_store_and_writer_share_lock_until_reader_closes(self):
        entered, release, writing = threading.Event(), threading.Event(), threading.Event()
        original = Path.read_text

        def slow_read(path, *args, **kwargs):
            if path != self.path:
                return original(path, *args, **kwargs)
            with path.open('r', encoding='utf-8') as stream:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test reader was not released')
                return stream.read()

        def write():
            writing.set()
            Store(self.temp.name).write('quota.json', dict(self.old, requests=8))

        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(Path, 'read_text', slow_read):
            reader = pool.submit(self.store.read, 'quota.json')
            writer = None
            try:
                self.assertTrue(entered.wait(2))
                writer = pool.submit(write)
                self.assertTrue(writing.wait(2))
                # 写入应等待读取句柄关闭，不得立刻因 WinError 5 失败。
                self.assertFalse(writer.done())
            finally:
                release.set()
            self.assertEqual(reader.result(timeout=3), self.old)
            writer.result(timeout=3)
        self.assertEqual(self.store.read('quota.json'), dict(self.old, requests=8))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows 文件共享行为')
    def test_real_windows_read_handle_releases_and_risk_pause_is_saved(self):
        handle = self.path.open('rb')
        released = threading.Timer(.15, handle.close)
        released.start()
        try:
            Guard(self.store, lambda message: None).record_risk(429, until=2100000000)
        finally:
            released.join(timeout=2)
            handle.close()
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['blocked_until'], 2100000000)
        self.assertTrue(saved['needs_verify'])
        self.assertEqual(saved['risks'], 1)
