# NPK test host boundaries

Run through `runtime\venv\Scripts\python.exe tools\run_tests.py`. The runner
allocates a new store in `build/test-runs`, then installs boundaries before any
application module is imported. Never point a test process at the desktop DATA.

The compatibility command `runtime\venv\Scripts\python.exe tests\run_npk_tests.py`
now delegates to this runner with the `media` profile by default, because the
full suite contains owned FFmpeg/Node fixture checks. It retains the same data,
credential, network and inference boundaries. Do not invoke plain
`python -m unittest discover` from the project root: application imports use
`ROOT/data` when `VIETDUB_DATA_DIR` was not set before startup.

## Profiles

| Profile | Real local processes | Inference | Network / credentials |
| --- | --- | --- | --- |
| `unit` (default) | Denied; use mocks | Denied at supported model constructors/loaders | Python socket/DNS denied; credentials stay in fixture RAM |
| `media` | Configured FFmpeg, ffprobe, Node; one guarded CPU preview fixture wrapper | Denied | Same Python and credential boundaries |
| `native` (explicit opt-in) | Media binaries plus Python under NPK runtime | Offline inference permitted | Python external sockets/DNS denied; loopback test server permitted; fixture credentials only |

Examples:

```powershell
& '.\runtime\venv\Scripts\python.exe' tools\run_tests.py --pattern test_provider_resilience.py --verbose
& '.\runtime\venv\Scripts\python.exe' tools\run_tests.py --profile media --verbose
```

The existing full suite contains real tiny media and Node checks, so use
`--profile media` for that suite. A default unit run that attempts a real process
fails visibly rather than silently starting a native worker. MockTransport and
patched inference constructors continue to work. Windows asyncio's bounded
socketpair implementation is permitted without permitting provider connections
to localhost.

Each completed run retains `test-boundary-report.json` beside its private
`studio.sqlite3`. It records the profile, whether installation preceded app
imports, process executable names and denied boundary categories. It does not
record keys, command bodies, prompts or HTTP request bodies. Expected rejection
tests produce `blocked` entries; those entries are not product defects.

These are Python application guards, **not an OS sandbox**. Media/native
profiles are explicit permission to execute trusted fixture code and binaries;
Python guards do not inspect a Node program or sandbox a native FFmpeg process.
Use fixture input and owned output paths for those profiles. The native profile
can read installed models and consume CPU/GPU, so it is never the default.
Production `ROOT/data` reads and SQLite connections are blocked in all profiles.
Unit/media profiles block Whisper/ONNX/CTranslate2 constructors and Torch model
loaders; real VieNeu native startup is blocked at process admission.

The sole Python exception in `media` is the exact command
`sys.executable -X utf8 tools/media_fixture_child.py`. That wrapper installs the
same boundaries before application imports and admits only Telea CPU previews
with source media under `tests/fixtures` or the run's private DATA. It exercises
the production preview rendering function in a real child without permitting
the general native preview script or loading a model. Model-mode and outside
source requests are tested rejections.
