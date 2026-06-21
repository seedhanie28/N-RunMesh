[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$EnvFile = Join-Path $RepoRoot ".env"
$ExampleFile = Join-Path $RepoRoot ".env.example"

function New-RandomHex([int]$ByteCount) {
    $bytes = New-Object byte[] $ByteCount
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    return -join ($bytes | ForEach-Object { $_.ToString("x2") })
}

function Set-EnvValue([string]$Key, [string]$Value) {
    $lines = Get-Content -LiteralPath $EnvFile
    $updated = $lines | ForEach-Object {
        if ($_.StartsWith("$Key=")) { "$Key=$Value" } else { $_ }
    }
    [IO.File]::WriteAllLines(
        $EnvFile,
        [string[]]$updated,
        (New-Object Text.UTF8Encoding($false))
    )
}

if (-not (Test-Path -LiteralPath $EnvFile)) {
    Copy-Item -LiteralPath $ExampleFile -Destination $EnvFile

    $databasePassword = New-RandomHex 24
    $secretKey = New-RandomHex 32
    $agentKey = New-RandomHex 32
    $adminPassword = New-RandomHex 12

    Set-EnvValue "POSTGRES_PASSWORD" $databasePassword
    Set-EnvValue "SECRET_KEY" $secretKey
    Set-EnvValue "CRON_AGENT_API_KEY" $agentKey
    Set-EnvValue "NRUNMESH_ADMIN_PASSWORD" $adminPassword

    Write-Host "Generated a new .env file."
    Write-Host "Initial login: admin / $adminPassword"
    Write-Host "Save this password now. It is not printed on later starts."
}

Push-Location $RepoRoot
try {
    $backupHostPath = "./backups"
    Get-Content -LiteralPath $EnvFile | ForEach-Object {
        if ($_ -match '^BACKUP_HOST_PATH=(.*)$') {
            $backupHostPath = $matches[1].Trim()
        }
    }
    if (-not [IO.Path]::IsPathRooted($backupHostPath)) {
        $backupHostPath = Join-Path $RepoRoot $backupHostPath
    }
    New-Item -ItemType Directory -Force -Path $backupHostPath | Out-Null

    docker compose up -d --build
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose startup failed." }
    docker compose ps
}
finally {
    Pop-Location
}
