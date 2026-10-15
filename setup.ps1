# Install only into this app's own folder. Never moves another application's data.
$ErrorActionPreference = 'Stop'
$studioRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$studioRuntime = Join-Path $studioRoot 'runtime'
$studioModels = Join-Path $studioRoot 'models'
New-Item -ItemType Directory -Path $studioRuntime,$studioModels -Force | Out-Null
$studioPython = Join-Path $studioRuntime 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $studioPython)) {
    $basePython = $env:NPK_BASE_PYTHON
    if (-not $basePython) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            $basePython = (& py -3.12 -c 'import sys; print(sys.executable)')
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            $basePython = (Get-Command python).Source
        }
    }
    if (-not $basePython) { throw 'Install Python 3.12 x64 or set NPK_BASE_PYTHON.' }
    & $basePython -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 required"'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 x64 is required.' }
    if (-not (Test-Path -LiteralPath $basePython)) { throw 'Install Python 3.12 x64, then update $basePython in setup.ps1.' }
    & $basePython -m venv (Join-Path $studioRuntime 'venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated Python environment.' }
}
$env:HF_HOME = Join-Path $studioModels 'huggingface'
$env:TORCH_HOME = Join-Path $studioModels 'torch'
$env:PIP_CACHE_DIR = Join-Path $studioRuntime 'pip-cache'
$env:TEMP = Join-Path $studioRuntime 'temp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
& $studioPython -m pip install -r (Join-Path $studioRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $studioPython -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu128 'torch==2.11.0+cu128'
if ($LASTEXITCODE -ne 0) { throw 'PyTorch installation failed.' }

function Get-StudioModel([string]$RelativePath, [string]$Url, [string]$ExpectedHash) {
    $destination = [IO.Path]::GetFullPath((Join-Path $studioModels $RelativePath))
    if (-not $destination.StartsWith($studioModels + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid model target.' }
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    if (Test-Path -LiteralPath $destination) {
        if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash -eq $ExpectedHash) { return }
        throw "Existing model differs from the verified version: $destination. Back it up before retrying."
    }
    $download = $destination + '.download'
    & curl.exe --fail --location --retry 2 --output $download $Url
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $RelativePath" }
    if ((Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash -ne $ExpectedHash) { throw "Model checksum mismatch: $RelativePath" }
    Move-Item -LiteralPath $download -Destination $destination
}
Get-StudioModel 'lama\big-lama.pt' 'https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt' '7BA7AA7AC37A4D41FDBBEBA3A2AF7EAD18058552997E3A3CD1A3B2210C9E6B4C'
Get-StudioModel 'piper\vi_VN-vais1000-medium.onnx' 'https://huggingface.co/rhasspy/piper-voices/resolve/main/vi/vi_VN/vais1000/medium/vi_VN-vais1000-medium.onnx' 'EC7C89E2C85F4D1EDC24B6120C18AAF1BDA614F06B511567EB9C7C0DE15E2DAB'
Get-StudioModel 'piper\vi_VN-vais1000-medium.onnx.json' 'https://huggingface.co/rhasspy/piper-voices/resolve/main/vi/vi_VN/vais1000/medium/vi_VN-vais1000-medium.onnx.json' 'FAFB9DA1354ED4B77C31AF228ED41FB41CD825C14CFFA105454B25E6AE751EE0'
Write-Host 'Ready. Launch VietDub Studio.exe, or runtime\venv\Scripts\python.exe app.py.'
Write-Host 'FFmpeg/ffprobe must remain in runtime\ffmpeg, or be available on PATH.'
