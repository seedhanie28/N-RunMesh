[CmdletBinding()]
param(
    [string]$Version = "0.1.0",
    [string]$PrivateKey = ".signing\engine_private_key.pem"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Agent = Join-Path $Root "agent"
$PrivateKeyPath = [IO.Path]::GetFullPath((Join-Path $Root $PrivateKey))

if (-not (Test-Path $PrivateKeyPath)) {
    throw "Private signing key not found: $PrivateKeyPath"
}

Push-Location $Root
try {
    Get-ChildItem "$Agent\app\executor*.so" -ErrorAction SilentlyContinue | Remove-Item -Force
    Get-ChildItem "$Agent\app\scheduler*.so" -ErrorAction SilentlyContinue | Remove-Item -Force
    Get-ChildItem "$Agent\agent\engine_verifier*.so" -ErrorAction SilentlyContinue | Remove-Item -Force
    docker build -f agent/engine/Dockerfile.linux -t nrunmesh-agent-engine-builder agent
    if ($LASTEXITCODE -ne 0) { throw "Linux engine build failed." }
    $container = docker create nrunmesh-agent-engine-builder
    if ($LASTEXITCODE -ne 0) { throw "Could not create engine builder container." }
    try {
        docker cp "${container}:/src/app/." "$Agent\app"
        if ($LASTEXITCODE -ne 0) { throw "Could not copy compiled Linux engine." }
        docker cp "${container}:/src/agent/." "$Agent\agent"
        if ($LASTEXITCODE -ne 0) { throw "Could not copy compiled engine verifier." }
    }
    finally {
        docker rm $container | Out-Null
    }

    py -3.12 -m pip install -q -r agent/engine/requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Could not install release build dependencies." }
    py -3.12 agent/engine/package_release.py `
        --private-key $PrivateKeyPath `
        --platform linux-x86_64 `
        --version $Version
    if ($LASTEXITCODE -ne 0) { throw "Could not package Linux agent release." }
}
finally {
    Pop-Location
}
