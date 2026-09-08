[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ArchivePath,
    [int]$TimeoutSeconds = 240
)

$ErrorActionPreference = "Stop"
$ArchivePath = (Resolve-Path $ArchivePath).Path
$SmokeRoot = Join-Path $env:RUNNER_TEMP (
    "portable smoke 便携测试 " + [guid]::NewGuid().ToString("N")
)
$ExtractRoot = Join-Path $SmokeRoot "extracted package"
$StandardOutput = Join-Path $SmokeRoot "launcher.stdout.log"
$StandardError = Join-Path $SmokeRoot "launcher.stderr.log"
New-Item -ItemType Directory -Path $ExtractRoot -Force | Out-Null
Expand-Archive -Path $ArchivePath -DestinationPath $ExtractRoot

$Executable = Get-ChildItem $ExtractRoot -Filter "LocalVideoCutter.exe" -File -Recurse |
    Select-Object -First 1
if (-not $Executable) {
    throw "LocalVideoCutter.exe was not found in the archive"
}
$PortableRoot = $Executable.Directory.FullName
$RuntimePath = Join-Path $PortableRoot "data\runtime\runtime.json"
$LauncherProcess = $null

$RequiredPortableFiles = @(
    "launcher_models.py",
    "app\ai_models.py",
    "vendor\swiftvr_runner.py",
    "vendor\SWIFTVR_LICENSE.txt",
    "requirements-ai-swiftvr-cuda.txt"
)
foreach ($RelativePath in $RequiredPortableFiles) {
    $RequiredPath = Join-Path $PortableRoot $RelativePath
    if (-not (Test-Path $RequiredPath -PathType Leaf)) {
        throw "Required portable package file is missing: $RelativePath"
    }
}

try {
    $LauncherProcess = Start-Process `
        -FilePath $Executable.FullName `
        -ArgumentList @("--no-browser", "--skip-model-prompt", "--port", "18777") `
        -WorkingDirectory $PortableRoot `
        -RedirectStandardOutput $StandardOutput `
        -RedirectStandardError $StandardError `
        -PassThru

    $Deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $Health = $null
    $Port = 0
    while ([DateTime]::UtcNow -lt $Deadline) {
        if ($LauncherProcess.HasExited) {
            break
        }
        if (Test-Path $RuntimePath) {
            try {
                $Runtime = Get-Content $RuntimePath -Raw -Encoding utf8 | ConvertFrom-Json
                $Port = [int]$Runtime.port
            }
            catch {
                $Port = 0
            }
        }
        if ($Port -le 0) {
            Start-Sleep -Milliseconds 500
            continue
        }
        try {
            $Health = Invoke-RestMethod `
                -Uri "http://127.0.0.1:$Port/api/health" `
                -TimeoutSec 2
            if (
                $Health.status -eq "ok" -and
                $Health.instance_id -eq $Runtime.instance_id
            ) {
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }

    if (
        -not $Health -or
        $Health.app_id -ne "com.seanli.local-video-cutter" -or
        $Health.instance_id -ne $Runtime.instance_id
    ) {
        throw "The packaged application did not pass its health check"
    }

    $Index = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -TimeoutSec 10
    if ($Index.Content -notmatch "本地视频剪辑") {
        throw "The packaged application did not serve its user interface"
    }
    $Bootstrap = Invoke-RestMethod `
        -Uri "http://127.0.0.1:$Port/api/bootstrap" `
        -TimeoutSec 10
    if (-not $Bootstrap.app_token) {
        throw "The packaged application did not provide an application token"
    }

    $ModelCatalog = Invoke-RestMethod `
        -Uri "http://127.0.0.1:$Port/api/ai-models" `
        -Headers @{ "X-App-Token" = [string]$Bootstrap.app_token } `
        -TimeoutSec 10
    $Models = @($ModelCatalog.models)
    $ExpectedModelIds = @(
        "seedvr2-3b-fp16",
        "swiftvr-5b-bf16",
        "flashvsr-v1-1-full"
    )
    foreach ($ExpectedModelId in $ExpectedModelIds) {
        $ModelMatches = @($Models | Where-Object { $_.id -eq $ExpectedModelId })
        if ($ModelMatches.Count -ne 1) {
            throw "The packaged AI model catalog is missing or duplicated: $ExpectedModelId"
        }
    }

    $SwiftModel = $Models | Where-Object { $_.id -eq "swiftvr-5b-bf16" }
    $SwiftTargets = @($SwiftModel.supported_targets)
    if ($SwiftTargets.Count -ne 1 -or [string]$SwiftTargets[0] -ne "1080p") {
        throw "SwiftVR must expose only the 1080p target in this release"
    }

    $FlashModel = $Models | Where-Object { $_.id -eq "flashvsr-v1-1-full" }
    if (
        $FlashModel.status -ne "blocked" -or
        -not [bool]$FlashModel.blocked -or
        [bool]$FlashModel.startup_prompt -or
        [bool]$FlashModel.include_in_startup_prompt
    ) {
        throw "FlashVSR must remain blocked and excluded from startup prompts"
    }

    if (-not $Bootstrap.ai_runtime.encoder_available) {
        throw "FFmpeg Full did not expose the required libx265 encoder"
    }
    if (-not $Bootstrap.ai_runtime.color_pipeline_available) {
        if (
            $Bootstrap.ai_runtime.hardware_detected -or
            $Bootstrap.ai_runtime.color_pipeline_error -eq "libplacebo or zscale is missing"
        ) {
            throw (
                "FFmpeg Full did not pass the packaged AI color-pipeline check: " +
                $Bootstrap.ai_runtime.color_pipeline_error
            )
        }
        Write-Output (
            "Runtime libplacebo execution requires a GPU and was not asserted on this runner: " +
            $Bootstrap.ai_runtime.color_pipeline_error
        )
    }

    if (-not (Test-Path $RuntimePath)) {
        throw "The portable runtime record was not created beside the executable"
    }
    $Runtime = Get-Content $RuntimePath -Raw -Encoding utf8 | ConvertFrom-Json
    $ExpectedRoot = [System.IO.Path]::GetFullPath($PortableRoot).TrimEnd("\")
    $ActualRoot = [System.IO.Path]::GetFullPath([string]$Runtime.project_root).TrimEnd("\")
    if ($ActualRoot -ine $ExpectedRoot) {
        throw "Runtime data escaped the portable root: $ActualRoot"
    }
    if (-not (Test-Path (Join-Path $PortableRoot ".venv\Scripts\python.exe"))) {
        throw "The packaged launcher did not prepare its local Python environment"
    }

    & $Executable.FullName --stop
    if ($LASTEXITCODE -ne 0) {
        throw "The packaged stop command failed with exit code $LASTEXITCODE"
    }
    if (-not $LauncherProcess.WaitForExit(20000)) {
        throw "The packaged launcher did not exit after the authenticated stop request"
    }
    if ($LauncherProcess.ExitCode -ne 0) {
        throw "The packaged launcher exited with code $($LauncherProcess.ExitCode)"
    }
    Write-Output "Portable package smoke test passed at $PortableRoot"
}
finally {
    if ($LauncherProcess -and -not $LauncherProcess.HasExited) {
        & taskkill.exe /PID $LauncherProcess.Id /T /F | Out-Null
    }
    if (Test-Path $StandardOutput) {
        Write-Output "Launcher standard output:"
        Get-Content $StandardOutput
    }
    if (Test-Path $StandardError) {
        Write-Output "Launcher standard error:"
        Get-Content $StandardError
    }
}
