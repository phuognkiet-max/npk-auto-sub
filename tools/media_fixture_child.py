"""Explicit media-only child for the production live-preview fixture test.

Calls the same production rendering function in a child, with guards installed
first. Only Telea CPU and an owned fixture/private video are admitted; the
general native preview entrypoint remains denied by the media test profile.
"""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.test_boundary import install


def main():
    if os.environ.get('NPK_TEST_PROFILE') != 'media':
        raise RuntimeError('The fixture preview child requires an explicit media test profile.')
    private = Path(os.environ['VIETDUB_DATA_DIR']).resolve()
    report = install(ROOT, private, 'media')
    try:
        spec = json.loads(sys.stdin.buffer.read())
        source = Path(spec['media']['path']).resolve(strict=True)
        roots = ((ROOT/'tests/fixtures').resolve(), private)
        if not any(source.is_relative_to(base) for base in roots) or spec['params']['method'] != 'telea':
            raise RuntimeError('The fixture child permits only owned media and the Telea CPU algorithm.')
        from contextlib import redirect_stdout
        from live_clean_preview import render_in_child
        with redirect_stdout(sys.stderr):
            output = render_in_child(spec)
        sys.stdout.buffer.write(output)
    finally:
        (private/'media-fixture-boundary-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
