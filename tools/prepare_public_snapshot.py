"""Export a reviewable source snapshot with an explicit, non-recursive allowlist.

Never copies application DATA, models, browser profiles, media or test output.
The original application and its local credentials/checkpoints are untouched.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
DOCS = ('ARCHITECTURE.md', 'AI_REVIEW_GUIDE.md', 'THIRD_PARTY.md', 'TEST-BOUNDARIES.md')
EXCLUDED_TESTS = {'quality_smoke.py', 'pipeline_npk.py', 'ui_npk.py'}


def export(output):
    output = Path(output).resolve()
    if output == ROOT or ROOT in output.parents or output in ROOT.parents:
        raise ValueError('Public snapshot must be a separate sibling checkout.')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Destination must be empty; never overwrite an existing checkout.')
    output.mkdir(parents=True, exist_ok=True)
    selected = list(ROOT.glob('*.py')) + [ROOT/'requirements.txt', ROOT/'setup.ps1']
    for name, suffixes in [('static', {'.html','.css','.js','.png','.ico'}),
                           ('tools', {'.py','.ps1'}), ('tests', {'.py','.cjs'})]:
        selected.extend(p for p in (ROOT/name).iterdir() if p.is_file() and p.suffix in suffixes
                        and not (name == 'tests' and p.name in EXCLUDED_TESTS)
                        and p.name != 'package-release.ps1')
    selected.extend(ROOT/'docs'/name for name in DOCS)
    selected.extend(ROOT/'assets/branding'/name for name in ('README.md','npk-auto-sub-master.png'))
    protected = re.search(r"PROTECTED_PROJECT_ID\s*=\s*['\"]([0-9a-f]{32})", (ROOT/'execution_admission.py').read_text(encoding='utf-8'))
    protected = protected.group(1) if protected else None
    manifest = []
    for source in sorted(set(selected)):
        if source.is_symlink() or not source.is_file():
            raise ValueError('Missing/unsafe source: '+str(source.relative_to(ROOT)))
        relative = source.relative_to(ROOT)
        dest = output/relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        normalized = False
        if source.suffix in ('.py','.ps1','.js','.cjs','.html','.css','.md','.txt'):
            original = source.read_text(encoding='utf-8-sig')
            content = original
            if protected:
                content = content.replace(protected, '00000000000000000000000000000001')
            local_user = re.escape(Path.home().name)
            content = re.sub(r'(?i)([CD]:[\\/]+(?:Users|UserData)[\\/]+)'+local_user+r'\b', r'\1developer', content)
            if source.name == 'setup.ps1':
                content = content.replace("$basePython = 'C:\\Users\\developer\\AppData\\Local\\Programs\\Python\\Python312\\python.exe'", """$basePython = $env:NPK_BASE_PYTHON
    if (-not $basePython) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            $basePython = (& py -3.12 -c 'import sys; print(sys.executable)')
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            $basePython = (Get-Command python).Source
        }
    }
    if (-not $basePython) { throw 'Install Python 3.12 x64 or set NPK_BASE_PYTHON.' }
    & $basePython -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 required"'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 x64 is required.' }""")
            normalized = content != original
            if source.suffix == '.py':
                ast.parse(content, filename=str(relative))
            dest.write_text(content, encoding='utf-8', newline='\n')
        else:
            shutil.copyfile(source, dest)
        manifest.append({'path':relative.as_posix(), 'bytes':dest.stat().st_size,
                         'sha256':hashlib.sha256(dest.read_bytes()).hexdigest(), 'normalized':normalized})
    (output/'PUBLIC_SOURCE_MANIFEST.json').write_text(json.dumps({
        'purpose':'Sanitized first-party source for discussion and review',
        'snapshot_date':'2026-10-15', 'files':manifest,
        'excluded':['data','models','runtime','build','dist','executables','media',
                    'browser profiles','generated test output','conversation exports',
                    'personal disk inventories','upstream reference checkout','historical package-release.ps1'],
        'normalization':['Local Windows account path becomes developer',
                         'Private protected project ID becomes synthetic 00000000000000000000000000000001',
                         'setup.ps1 discovers Python 3.12 instead of a private absolute path'],
        'license':'No new open-source license grant is implied. See docs/THIRD_PARTY.md.'},
        ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'output':str(output),'source_files':len(manifest),
                      'source_bytes':sum(item['bytes'] for item in manifest)},ensure_ascii=False))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    export(parser.parse_args().output)
