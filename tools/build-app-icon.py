"""Encode the approved bitmap as Windows/HTML icon assets, preserving alpha.

This is mechanical packaging, not redrawing or altering the generated design.
"""
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MASTER = ROOT / 'assets/branding/npk-auto-sub-master.png'
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def main():
    with Image.open(MASTER) as source:
        if source.width != source.height or source.mode != 'RGBA':
            raise ValueError('Icon master must be a square RGBA image.')
        if source.getchannel('A').getextrema() != (0, 255):
            raise ValueError('Preserve the original transparent exterior.')
        source.save(ROOT / 'static/npk-app-icon.ico', format='ICO', sizes=[(s, s) for s in SIZES])
        source.resize((256, 256), Image.Resampling.LANCZOS).save(ROOT / 'static/npk-app-icon.png')
    with Image.open(ROOT / 'static/npk-app-icon.ico') as icon:
        assert icon.ico.sizes() == {(s, s) for s in SIZES}
        print('Windows icon sizes:', sorted(icon.ico.sizes()))


if __name__ == '__main__':
    main()
