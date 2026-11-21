# Install only the current user's interactive task. It remains disabled.
$ErrorActionPreference = 'Stop'
$taskName = 'OpenChamber Voice'
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent()

# A folder unpacked from a downloaded zip is marked "from the Internet", which
# blocks unsigned .ps1 files; clear it for the files this app runs.
try {
    foreach ($file in Get-ChildItem -LiteralPath $PSScriptRoot -File) {
        if ($file.Extension -eq '.ps1' -or $file.Extension -eq '.py') {
            Unblock-File -LiteralPath $file.FullName -ErrorAction SilentlyContinue
        }
    }
} catch {
    Write-Host "Could not clear the Internet mark: $($_.Exception.Message)"
}

try {
    $existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
    if ($existing) {
        $owner = $existing.Principal.UserId
        try {
            $sid = ([System.Security.Principal.NTAccount]::new($owner)).Translate([System.Security.Principal.SecurityIdentifier]).Value
        } catch {
            $sid = $owner
        }
        if ($sid -ne $user.User.Value) { throw "Refusing to replace task owned by $owner" }
    }
    $python = 'C:\Program Files\Python313\pythonw.exe'
    if (!(Test-Path -LiteralPath $python)) {
        $found = Get-Command pythonw.exe -ErrorAction SilentlyContinue
        if ($found) { $python = $found.Source }
    }
    $guard = Join-Path $PSScriptRoot 'guard.py'
    if (!(Test-Path -LiteralPath $guard)) { throw 'guard.py is missing from this folder' }
    if (!(Test-Path -LiteralPath $python)) { throw "pythonw.exe not found ('$python'). Install Python 3.13 or edit the path in install-guard.ps1" }
    $action = New-ScheduledTaskAction -Execute $python -Argument ('"' + $guard + '"') -WorkingDirectory $PSScriptRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user.Name
    $principal = New-ScheduledTaskPrincipal -UserId $user.Name -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Disable
    $settings.Priority = 4
    Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'OpenChamber Voice supervisor' -Force | Out-Null
    Write-Host "Installed disabled with $python. When ready: Enable-ScheduledTask -TaskName `"$taskName`""
} catch {
    Write-Host "Install failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'If PowerShell blocks this script, run it once with: powershell -NoProfile -ExecutionPolicy Bypass -File .\install-guard.ps1'
    exit 1
}
