param(
    [string]$PythonExe
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$release = Join-Path $project 'release'

if ($PythonExe) {
    $pythonExe = (Resolve-Path -LiteralPath $PythonExe -ErrorAction Stop).Path
} else {
    $pythonExe = Join-Path $project '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        $pythonExe = (Get-Command python -ErrorAction Stop).Source
    }
}

& $pythonExe (Join-Path $project 'make_icon.py')
if ($LASTEXITCODE -ne 0) { throw '產生圖示失敗。' }
$previousQtPlatform = $env:QT_QPA_PLATFORM
$previousDataDir = $env:QUOTA_PROMPTDOCK_DATA_DIR
$isolatedDataDir = Join-Path $project ("build\verification-{0}" -f [guid]::NewGuid().ToString("N"))
try {
    $env:QUOTA_PROMPTDOCK_DATA_DIR = $isolatedDataDir
    $env:QT_QPA_PLATFORM = 'offscreen'
    & $pythonExe -m pytest $project -q
    if ($LASTEXITCODE -ne 0) { throw '測試失敗，停止打包。' }
} finally {
    $env:QT_QPA_PLATFORM = $previousQtPlatform
    $env:QUOTA_PROMPTDOCK_DATA_DIR = $previousDataDir
}

# 固定原生 DLL 搜尋路徑，避免 PATH 中其他工具的 ICU 等同名 DLL 混入。
$previousPath = $env:PATH
try {
    $pythonFolder = Split-Path -Parent $pythonExe
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;$pythonFolder"
    & $pythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name 'QuotaDock' `
    --icon (Join-Path $project 'codex_usage.ico') `
    --distpath $release `
    --workpath (Join-Path $project 'build') `
    --specpath $project `
    (Join-Path $project 'app.py')
    if ($LASTEXITCODE -ne 0) { throw '打包失敗。' }
} finally {
    $env:PATH = $previousPath
}

$executable = Join-Path $release 'QuotaDock.exe'
$smokePath = Join-Path $release ("smoke-{0}.png" -f [guid]::NewGuid().ToString('N'))
$previousQtPlatform = $env:QT_QPA_PLATFORM
$previousDataDir = $env:QUOTA_PROMPTDOCK_DATA_DIR
$isolatedDataDir = Join-Path $project ("build\verification-{0}" -f [guid]::NewGuid().ToString("N"))
try {
    $env:QUOTA_PROMPTDOCK_DATA_DIR = $isolatedDataDir
    $env:QT_QPA_PLATFORM = 'offscreen'
    $smoke = Start-Process -FilePath $executable `
        -ArgumentList @('--demo', '--screenshot', ('"{0}"' -f $smokePath)) `
        -PassThru -WindowStyle Hidden
    try {
        if (-not $smoke.WaitForExit(60000)) {
            $smoke.Kill()
            $smoke.WaitForExit()
            throw '打包執行檔展示啟動逾時。'
        }
        if ($smoke.ExitCode -ne 0) {
            throw "打包執行檔展示啟動失敗，結束碼：$($smoke.ExitCode)"
        }
    } finally {
        $smoke.Dispose()
    }
    if (-not (Test-Path -LiteralPath $smokePath) -or (Get-Item -LiteralPath $smokePath).Length -eq 0) {
        throw '打包執行檔未產生展示截圖。'
    }
} finally {
    $env:QT_QPA_PLATFORM = $previousQtPlatform
    $env:QUOTA_PROMPTDOCK_DATA_DIR = $previousDataDir
    Remove-Item -LiteralPath $smokePath -ErrorAction SilentlyContinue
}

$publicExecutable = Join-Path $release 'QuotaDock-Windows-x64.exe'
Copy-Item -LiteralPath $executable -Destination $publicExecutable -Force

$licenseArchive = Join-Path $release 'QuotaDock-Licenses.zip'
$licenseFiles = @(
    (Join-Path $project 'LICENSE'),
    (Join-Path $project '第三方元件.md'),
    (Join-Path $project 'licenses')
)
Compress-Archive -LiteralPath $licenseFiles -DestinationPath $licenseArchive -Force

$checksumPath = Join-Path $release 'QuotaDock-SHA256SUMS.txt'
[string[]]$checksums = @($publicExecutable, $licenseArchive) | ForEach-Object {
    '{0}  {1}' -f (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant(), (Split-Path -Leaf $_)
}
[System.IO.File]::WriteAllLines($checksumPath, $checksums, [System.Text.Encoding]::ASCII)

Write-Output $publicExecutable
Write-Output $licenseArchive
Write-Output $checksumPath
