"""Verify the runner's installed boundaries; never probe a real credential."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

import keyring
from tools.test_boundary import BoundaryViolation

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('NPK_TEST_BOUNDARY_ACTIVE'), 'Use tools/run_tests.py to install boundaries before imports.')
class TestBoundaryTests(unittest.TestCase):
    def test_windows_credential_entrypoints_are_fixture_memory_only(self):
        from keyring.backend import KeyringBackend
        class ForbiddenBackend(KeyringBackend):
            priority = 1
            def get_password(self, *args):
                self.fail('Native credential backend must not be called')
            def set_password(self, *args):
                raise AssertionError('Native credential backend must not be called')
            def delete_password(self, *args):
                raise AssertionError('Native credential backend must not be called')
        previous = keyring.get_keyring()
        try:
            with self.assertRaises(BoundaryViolation):
                keyring.set_keyring(ForbiddenBackend())
            keyring.set_password('npk-test-only', 'fixture', 'synthetic-fixture-secret')
            self.assertEqual(keyring.get_password('npk-test-only', 'fixture'), 'synthetic-fixture-secret')
            keyring.delete_password('npk-test-only', 'fixture')
            self.assertIsNone(keyring.get_password('npk-test-only', 'fixture'))
        finally:
            keyring.set_keyring(previous)

    def test_production_database_blocked_before_connection(self):
        with self.assertRaises(BoundaryViolation):
            sqlite3.connect(str(ROOT/'data/studio.sqlite3'))
        with self.assertRaises(BoundaryViolation):
            sqlite3.connect((ROOT/'data/studio.sqlite3').as_uri() + '?mode=ro', uri=True)

    def test_private_database_is_allowed(self):
        with sqlite3.connect(str(Path(os.environ['VIETDUB_DATA_DIR'])/'boundary-fixture.sqlite3')) as db:
            self.assertEqual(db.execute('select 1').fetchone(), (1,))

    def test_external_and_loopback_provider_connections_are_blocked(self):
        with self.assertRaises(BoundaryViolation):
            socket.create_connection(('example.invalid', 443), timeout=.01)
        if os.environ['NPK_TEST_BOUNDARY_ACTIVE'] != 'native':
            with self.assertRaises(BoundaryViolation):
                socket.create_connection(('127.0.0.1', 9), timeout=.01)

    def test_windows_asyncio_socketpair_is_allowed(self):
        first, second = socket.socketpair()
        try:
            first.sendall(b'x')
            self.assertEqual(second.recv(1), b'x')
        finally:
            first.close()
            second.close()
        async def probe():
            await asyncio.sleep(0)
            return 7
        self.assertEqual(asyncio.run(probe()), 7)

    def test_arbitrary_native_process_denied(self):
        with self.assertRaises(BoundaryViolation):
            subprocess.run(['C:/Windows/System32/cmd.exe', '/c', 'echo', 'fixture'], check=True)

    @unittest.skipUnless(os.environ.get('NPK_TEST_BOUNDARY_ACTIVE') == 'media', 'Explicit media fixture profile only')
    def test_owned_media_python_wrapper_rejects_model_mode_and_nonfixture_source(self):
        wrapper = ROOT/'tools/media_fixture_child.py'
        cases = ((ROOT/'tests/fixtures/demo.mp4', 'lama'), (ROOT/'README.md', 'telea'))
        for path, method in cases:
            with self.subTest(method=method):
                completed = subprocess.run([sys.executable, '-X', 'utf8', str(wrapper)],
                    input=json.dumps({'media': {'path': str(path)}, 'params': {'method': method}}),
                    capture_output=True, text=True, timeout=15)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn('only owned media and the Telea CPU algorithm', completed.stderr)

    def test_real_inference_constructor_denied_but_mocks_work(self):
        if os.environ['NPK_TEST_BOUNDARY_ACTIVE'] == 'native':
            self.skipTest('native profile is an explicit inference opt-in')
        import onnxruntime
        with self.assertRaises(BoundaryViolation):
            onnxruntime.InferenceSession('never-open-a-real-model.onnx')
        with patch('onnxruntime.InferenceSession', return_value='fixture'):
            self.assertEqual(onnxruntime.InferenceSession('synthetic.onnx'), 'fixture')


if __name__ == '__main__':
    unittest.main()
