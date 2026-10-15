"""Compatibility entrypoint for the isolated full-suite media runner."""
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    options = sys.argv[1:]
    if not any(option == '--profile' or option.startswith('--profile=') for option in options):
        options = ['--profile', 'media', *options]
    return subprocess.call([sys.executable, '-X', 'utf8',
                            str(root/'tools/run_tests.py'), *options], cwd=root)


if __name__ == '__main__':
    raise SystemExit(main())
