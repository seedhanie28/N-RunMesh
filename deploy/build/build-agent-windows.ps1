[CmdletBinding()]
param(
    [string]$Version = "0.1.0",
    [string]$PrivateKey = ".signing\engine_private_key.pem"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$PrivateKeyPath = [IO.Path]::GetFullPath((Join-Path $Root $PrivateKey))

if (-not (Test-Path $PrivateKeyPath)) {
    throw "Private signing key not found: $PrivateKeyPath"
}

Push-Location (Join-Path $Root "agent")
try {
    Get-ChildItem "app\executor*.pyd" -ErrorAction SilentlyContinue | Remove-Item -Force
    Get-ChildItem "app\scheduler*.pyd" -ErrorAction SilentlyContinue | Remove-Item -Force
    Get-ChildItem "agent\engine_verifier*.pyd" -ErrorAction SilentlyContinue | Remove-Item -Force
    py -3.12 -m pip install -q -r engine/requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Could not install Windows build dependencies." }
    $VcVars = "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
    if (-not (Test-Path $VcVars)) {
        throw "Microsoft C++ Build Tools 2022 with the VCTools workload is required."
    }
    $BuildCommand = "`"$VcVars`" && set DISTUTILS_USE_SDK=1 && set MSSdk=1 && py -3.12 engine\setup.py build_ext --inplace"
    & cmd.exe /d /s /c $BuildCommand
    if ($LASTEXITCODE -ne 0) {
        throw "Windows engine build failed. Install Microsoft C++ Build Tools 14 or newer."
    }
    py -3.12 engine/package_release.py `
        --private-key $PrivateKeyPath `
        --platform windows-x86_64 `
        --version $Version
    if ($LASTEXITCODE -ne 0) { throw "Could not package Windows agent release." }

    $PayloadZip = Join-Path $Root "dist\nrunmesh-agent-$Version-windows-x86_64.zip"
    $SetupExe = Join-Path $Root "dist\N-RunMesh-Agent-Setup-$Version.exe"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
        (Join-Path $Root "deploy\build\build-windows-setup.ps1") `
        -PayloadZip $PayloadZip `
        -Output $SetupExe
    if ($LASTEXITCODE -ne 0) { throw "Could not build Windows Setup.exe." }
    Remove-Item -Force $PayloadZip
}
finally {
    Pop-Location
}
