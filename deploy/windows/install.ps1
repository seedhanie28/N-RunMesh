[CmdletBinding()]
param(
    [ValidateSet("manual", "automatic")]
    [string]$Mode,
    [string]$Name = $env:COMPUTERNAME,
    [string]$ControllerUrl,
    [string]$SetupToken = "",
    [int]$Interval = 15,
    [string]$InstallDir,
    [string]$ConfigFile,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
$ControllerUrl = if ($ControllerUrl) { $ControllerUrl } else { $env:NRUNMESH_CONTROLLER_URL }
$SetupToken = if ($SetupToken) { $SetupToken } else { $env:NRUNMESH_SETUP_TOKEN }
$Name = if ($PSBoundParameters.ContainsKey("Name")) { $Name } elseif ($env:NRUNMESH_AGENT_NAME) { $env:NRUNMESH_AGENT_NAME } else { $Name }
if (-not $Mode -and $env:NRUNMESH_INSTALL_MODE) { $Mode = $env:NRUNMESH_INSTALL_MODE }
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$SourceDir = Join-Path $RepoRoot "agent"

if (-not (Test-Path (Join-Path $SourceDir "agent\cron_agent.py"))) {
    throw "Agent source not found at $SourceDir"
}
if (
    -not (Test-Path (Join-Path $SourceDir "engine-manifest.json")) -or
    -not (Test-Path (Join-Path $SourceDir "engine-manifest.sig"))
) {
    throw "Official compiled engine manifest is missing. Install from a packaged N-RunMesh Agent release."
}

if (-not $Mode) {
    if ($NonInteractive) {
        throw "-Mode is required with -NonInteractive."
    }
    Write-Host "Choose installation mode:"
    Write-Host "  1) manual    - install only; you start the agent"
    Write-Host "  2) automatic - install and start with Windows"
    $choice = Read-Host "Mode [1]"
    if (-not $choice) { $choice = "1" }
    switch ($choice.ToLowerInvariant()) {
        { $_ -in @("1", "manual") } { $Mode = "manual"; break }
        { $_ -in @("2", "automatic") } { $Mode = "automatic"; break }
        default { throw "Invalid mode." }
    }
}

if (-not $ControllerUrl) {
    if ($NonInteractive) {
        throw "-ControllerUrl is required with -NonInteractive."
    }
    $ControllerUrl = Read-Host "Controller URL (example: https://runmesh.example.com)"
}
if (-not $ControllerUrl) {
    throw "Controller URL is required."
}

if (-not $NonInteractive -and -not $PSBoundParameters.ContainsKey("Name")) {
    $inputName = Read-Host "Agent name [$Name]"
    if ($inputName) { $Name = $inputName }
}

if (-not $NonInteractive -and -not $PSBoundParameters.ContainsKey("SetupToken")) {
    $secureKey = Read-Host "One-time setup token" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try {
        $SetupToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}
if (-not $SetupToken) {
    throw "-SetupToken is required."
}

if (-not $InstallDir) {
    if ($Mode -eq "automatic") {
        $InstallDir = Join-Path $env:ProgramData "N-RunMesh\Agent"
    }
    else {
        $InstallDir = Join-Path $env:LOCALAPPDATA "N-RunMesh\Agent"
    }
}

if (-not $ConfigFile) {
    $ConfigFile = Join-Path $InstallDir "config\agent.env"
}

if ($Mode -eq "automatic") {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        throw "Automatic mode requires an Administrator PowerShell window."
    }
}

$pythonCommand = $null
$pythonPrefix = @()
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncher) {
    & $pyLauncher.Source -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $pythonCommand = $pyLauncher.Source
        $pythonPrefix = @("-3.12")
    }
}
if (-not $pythonCommand) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        & $python.Source -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $pythonCommand = $python.Source
        }
    }
}

if (-not $pythonCommand) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "Python is missing and winget is unavailable. Install Python 3.12, then rerun this installer."
    }
    Write-Host "Python is not installed; installing Python 3.12 automatically..."
    & $winget.Source install --id Python.Python.3.12 --exact --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Automatic Python installation failed." }
    $pythonCommand = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (-not (Test-Path $pythonCommand)) {
        throw "Python was installed but could not be located. Open a new PowerShell window and rerun the installer."
    }
}

& $pythonCommand @pythonPrefix -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.12 is required by this Agent release." }

Write-Host "Installing N-RunMesh Agent to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ConfigFile) | Out-Null

foreach ($folder in @("app", "agent")) {
    $target = Join-Path $InstallDir $folder
    if (Test-Path $target) { Remove-Item -Recurse -Force -LiteralPath $target }
    Copy-Item -Recurse -Force (Join-Path $SourceDir $folder) $target
}
Get-ChildItem -Path (Join-Path $InstallDir "app"), (Join-Path $InstallDir "agent") -Recurse -Force |
    Where-Object { $_.Name -like "._*" -or $_.Name -eq "__pycache__" } |
    Sort-Object FullName -Descending |
    Remove-Item -Recurse -Force
Copy-Item -Force (Join-Path $SourceDir "requirements-agent.txt") (Join-Path $InstallDir "requirements.txt")
foreach ($artifact in @("engine-manifest.json", "engine-manifest.sig", "engine_public_key.pem")) {
    $artifactPath = Join-Path $SourceDir $artifact
    if (Test-Path $artifactPath) {
        Copy-Item -Force $artifactPath (Join-Path $InstallDir $artifact)
    }
}

$venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    & $pythonCommand @pythonPrefix -m venv (Join-Path $InstallDir ".venv")
}
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $InstallDir "requirements.txt")

& $venvPython "$InstallDir\agent\cron_agent.py" register `
    --controller-url $ControllerUrl `
    --registration-token $SetupToken `
    --agent-name $Name `
    --config-file $ConfigFile
if ($LASTEXITCODE -ne 0) { throw "Agent registration failed." }

if ($Mode -eq "automatic") {
    & icacls.exe $ConfigFile /inheritance:r /grant:r "*S-1-5-18:F" "*S-1-5-32-544:F" | Out-Null
}
else {
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $ConfigFile /inheritance:r /grant:r "*${currentSid}:F" | Out-Null
}
if ($LASTEXITCODE -ne 0) {
    throw "Could not secure the Agent configuration file ACL."
}

$launcher = Join-Path $InstallDir "nrunmesh-agent.ps1"
@"
Set-Location "$InstallDir"
& "$venvPython" "$InstallDir\agent\cron_agent.py" run --config-file "$ConfigFile"
exit `$LASTEXITCODE
"@ | Set-Content -Encoding UTF8 $launcher

$cmdLauncher = Join-Path $InstallDir "nrunmesh-agent.cmd"
@"
@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$launcher"
"@ | Set-Content -Encoding ASCII $cmdLauncher

if ($Mode -eq "automatic") {
    $taskName = "N-RunMesh Agent"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Host "Automatic installation complete."
    Write-Host "Task:   $taskName"
    Write-Host "Config: $ConfigFile"
}
else {
    Write-Host "Manual installation complete."
    Write-Host "Start:  $cmdLauncher"
    Write-Host "Config: $ConfigFile"
}
