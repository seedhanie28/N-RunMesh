[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$PayloadZip,
    [Parameter(Mandatory)]
    [string]$Output
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Source = Join-Path $Root "deploy\windows\bootstrap\Program.cs"
$Manifest = Join-Path $Root "deploy\windows\bootstrap\app.manifest"
$Payload = (Resolve-Path $PayloadZip).Path
$OutputPath = if ([IO.Path]::IsPathRooted($Output)) {
    [IO.Path]::GetFullPath($Output)
}
else {
    [IO.Path]::GetFullPath((Join-Path $Root $Output))
}
$Csc = Get-ChildItem "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" -ErrorAction Stop |
    Select-Object -First 1 -ExpandProperty FullName

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null
& $Csc /nologo /target:winexe /optimize+ `
    /out:$OutputPath `
    /win32manifest:$Manifest `
    /resource:"$Payload,NRunMesh.Payload" `
    /reference:System.dll `
    /reference:System.Drawing.dll `
    /reference:System.Windows.Forms.dll `
    /reference:System.IO.Compression.dll `
    /reference:System.IO.Compression.FileSystem.dll `
    $Source
if ($LASTEXITCODE -ne 0) { throw "Could not compile Windows Setup.exe." }

Write-Output $OutputPath
