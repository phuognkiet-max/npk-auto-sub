import ctypes
import os
from pathlib import Path
import unittest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


class AppIconTests(unittest.TestCase):
    def test_ico_has_taskbar_and_dpi_sizes_with_alpha(self):
        with Image.open(ROOT / 'static/npk-app-icon.ico') as icon:
            self.assertEqual(icon.ico.sizes(), {(s,s) for s in (16,20,24,32,40,48,64,96,128,256)})
            for size in icon.ico.sizes():
                frame = icon.ico.getimage(size).convert('RGBA')
                self.assertEqual(frame.getchannel('A').getextrema(), (0,255))

    def test_header_asset_is_small_and_transparent(self):
        with Image.open(ROOT / 'static/npk-app-icon.png') as icon:
            self.assertEqual(icon.size, (256,256))
            self.assertEqual(icon.mode, 'RGBA')
        self.assertIn('/static/npk-app-icon.ico', (ROOT / 'static/studio.html').read_text(encoding='utf-8'))

    def test_taskbar_launches_npk_not_shared_python(self):
        from windows_identity import APP_ID, identity_properties
        props = identity_properties()
        self.assertEqual(props['ID'], APP_ID)
        self.assertEqual(props['RelaunchCommand'], f'"{ROOT / "NPK Auto Sub.exe"}"')
        self.assertTrue(Path(props['RelaunchIconResource'][:-2]).is_file())

    @unittest.skipUnless(os.name == 'nt', 'Windows ABI')
    def test_property_abi_and_foreign_window_guard(self):
        from windows_identity import GUID, PropVariant, WindowProperties
        self.assertEqual(ctypes.sizeof(GUID), 16)
        self.assertEqual(ctypes.sizeof(PropVariant), 24 if ctypes.sizeof(ctypes.c_void_p)==8 else 16)
        with self.assertRaisesRegex(ValueError, 'outside NPK'):
            WindowProperties(0)
