# Creates "AI Amalgam" shortcuts on the Desktop and in the Start menu.
$ErrorActionPreference = 'Stop'
$app = Split-Path -Parent $PSScriptRoot

$pyw = $null
foreach ($name in @('pyw', 'pythonw')) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { $pyw = $cmd.Source; break }
}
if (-not $pyw) {
    Write-Host "Couldn't find Python on this PC (looked for 'pyw' and 'pythonw')." -ForegroundColor Red
    Write-Host "Install Python from python.org, then run this again."
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$places = @(
    [Environment]::GetFolderPath('Desktop'),
    (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs')
)
foreach ($dir in $places) {
    $link = $shell.CreateShortcut((Join-Path $dir 'AI Amalgam.lnk'))
    $link.TargetPath = $pyw
    $link.Arguments = '"' + (Join-Path $app 'app.py') + '"'
    $link.WorkingDirectory = $app
    $link.IconLocation = (Join-Path $app 'amalgam.ico') + ',0'
    $link.Description = 'AI Amalgam - Claude and GPT in one chat'
    $link.Save()
}
Write-Host ""
Write-Host "Done! 'AI Amalgam' is now on your Desktop and in the Start menu." -ForegroundColor Green
Write-Host "Tip: right-click it in the Start menu and choose 'Pin to taskbar'."
