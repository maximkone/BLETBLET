# Install only the current user's interactive task. It remains disabled.
$ErrorActionPreference = 'Stop'
$taskName = 'OpenChamber Voice'
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent()
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
$guard = Join-Path $PSScriptRoot 'guard.py'
if (!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath $guard)) { throw 'Python or guard.py missing' }
$action = New-ScheduledTaskAction -Execute $python -Argument ('"' + $guard + '"') -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user.Name
$principal = New-ScheduledTaskPrincipal -UserId $user.Name -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Disable
$settings.Priority = 4
Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'OpenChamber Voice supervisor' -Force | Out-Null
Write-Host 'Installed disabled. When ready: Enable-ScheduledTask -TaskName "OpenChamber Voice"'
