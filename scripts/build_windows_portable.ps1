[CmdletBinding()]
param(
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $RepositoryRoot "release"
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
$Version = (& python -c "from app.build_info import APP_VERSION; print(APP_VERSION)").Trim()
if (-not $Version) {
    throw "Application version could not be resolved"
}

$PackageName = "LocalVideoCutter-Windows-RTX5090-v$Version"
$StageDirectory = Join-Path $OutputDirectory $PackageName
$ArchivePath = Join-Path $OutputDirectory "$PackageName.zip"
$ChecksumPath = "$ArchivePath.sha256"
foreach ($Path in @($StageDirectory, $ArchivePath, $ChecksumPath)) {
    if (Test-Path $Path) {
        throw "Build output already exists: $Path"
    }
}

$TemporaryBuildRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    "local-video-cutter-" + [guid]::NewGuid().ToString("N")
)
New-Item -ItemType Directory -Path $TemporaryBuildRoot | Out-Null
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $StageDirectory -Force | Out-Null

try {
    Push-Location $RepositoryRoot
    try {
        & python -m PyInstaller `
            --noconfirm `
            --clean `
            --onefile `
            --console `
            --name LocalVideoCutter `
            --paths $RepositoryRoot `
            --distpath (Join-Path $TemporaryBuildRoot "dist") `
            --workpath (Join-Path $TemporaryBuildRoot "work") `
            --specpath (Join-Path $TemporaryBuildRoot "spec") `
            (Join-Path $RepositoryRoot "windows_exe.py")
        if ($LASTEXITCODE -ne 0) {
            throw "PyInstaller failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }

    Copy-Item `
        (Join-Path $TemporaryBuildRoot "dist\LocalVideoCutter.exe") `
        (Join-Path $StageDirectory "LocalVideoCutter.exe")

    foreach ($DirectoryName in @("app", "vendor")) {
        $Destination = Join-Path $StageDirectory $DirectoryName
        New-Item -ItemType Directory -Path $Destination | Out-Null
        Copy-Item `
            (Join-Path $RepositoryRoot "$DirectoryName\*") `
            $Destination `
            -Recurse
    }

    $SourceFiles = @(
        "run.py",
        "stop.py",
        "launcher_windows.py",
        "launcher_models.py",
        "start.bat",
        "stop.bat",
        "requirements.txt",
        "requirements-ai.txt",
        "requirements-ai-common.txt",
        "requirements-ai-cuda.txt",
        "requirements-ai-swiftvr-cuda.txt",
        "README.md",
        "WINDOWS_README.txt",
        "WINDOWS_OFFLINE_README.txt",
        "LICENSE"
    )
    foreach ($FileName in $SourceFiles) {
        Copy-Item `
            (Join-Path $RepositoryRoot $FileName) `
            (Join-Path $StageDirectory $FileName)
    }

    Get-ChildItem $StageDirectory -Directory -Filter "__pycache__" -Recurse |
        Remove-Item -Recurse -Force
    Get-ChildItem $StageDirectory -File -Include "*.pyc", "*.pyo" -Recurse |
        Remove-Item -Force

    # Runtime data and multi-gigabyte offline payloads are distributed separately.
    foreach ($ExcludedDirectoryName in @("offline", "data")) {
        $ExcludedPath = Join-Path $StageDirectory $ExcludedDirectoryName
        if (Test-Path $ExcludedPath) {
            throw "Portable package must not include generated asset directory: $ExcludedPath"
        }
    }

    Compress-Archive -Path $StageDirectory -DestinationPath $ArchivePath -CompressionLevel Optimal
    $Hash = (Get-FileHash -Path $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $ChecksumLine = "$Hash  $([System.IO.Path]::GetFileName($ArchivePath))`n"
    [System.IO.File]::WriteAllText(
        $ChecksumPath,
        $ChecksumLine,
        [System.Text.Encoding]::ASCII
    )

    if ($env:GITHUB_OUTPUT) {
        "version=$Version" | Add-Content -Path $env:GITHUB_OUTPUT -Encoding utf8
        "package_name=$PackageName" | Add-Content -Path $env:GITHUB_OUTPUT -Encoding utf8
        "archive=$ArchivePath" | Add-Content -Path $env:GITHUB_OUTPUT -Encoding utf8
        "checksum=$ChecksumPath" | Add-Content -Path $env:GITHUB_OUTPUT -Encoding utf8
    }
    Write-Output "Created $ArchivePath"
    Write-Output "SHA256 $Hash"
}
finally {
    if (Test-Path $TemporaryBuildRoot) {
        Remove-Item $TemporaryBuildRoot -Recurse -Force
    }
}
