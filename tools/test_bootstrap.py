"""Install host boundaries before unittest imports any application module."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.test_boundary import install, write_report


if __name__ == '__main__':
    report = install(ROOT, os.environ['VIETDUB_DATA_DIR'], os.environ['NPK_TEST_PROFILE'])
    try:
        import unittest
        program = unittest.main(module=None, argv=['unittest', *sys.argv[1:]], exit=False)
        result = program.result
    finally:
        write_report(report)
    raise SystemExit(0 if result.wasSuccessful() else 1)
