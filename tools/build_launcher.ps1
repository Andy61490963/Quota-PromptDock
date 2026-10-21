param([string]$OutputPath)
$ErrorActionPreference = 'Stop'
$launcherProject = Split-Path -Parent $PSScriptRoot
if (-not $OutputPath) { $OutputPath = Join-Path $launcherProject 'build\launcher\QuotaDockLauncher.exe' }
$compilerPath = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compilerPath)) { throw 'Windows .NET Framework C# compiler was not found.' }
New-Item -ItemType Directory -Path (Split-Path -Parent $OutputPath) -Force | Out-Null
& $compilerPath /nologo /target:winexe /optimize+ /platform:anycpu /codepage:65001 `
    /reference:System.Runtime.Serialization.dll /reference:System.Windows.Forms.dll `
    ('/win32icon:' + (Join-Path $launcherProject 'codex_usage.ico')) `
    ('/out:' + $OutputPath) (Join-Path $PSScriptRoot 'QuotaDockLauncher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Stable launcher compilation failed.' }
Write-Output $OutputPath
