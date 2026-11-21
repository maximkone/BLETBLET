# Show an existing worker or ask Task Scheduler to start the supervisor.
#
# A folder unpacked from a downloaded .zip keeps the Windows "from the Internet"
# mark on every file, and the default RemoteSigned policy then refuses to load
# the unsigned .ps1 files here. Start.cmd passes -ExecutionPolicy Bypass, and
# this script clears the mark on its own files, so later runs need no switch.

$ErrorActionPreference = 'Stop'
$name = 'OpenChamber Voice'
$path = '\'

# Best effort: never let this step stop the worker from starting.
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
    $task = Get-ScheduledTask -TaskName $name -TaskPath $path -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "Task '$name' is not installed for this user." -ForegroundColor Yellow
        Write-Host 'Run Install.cmd once (it stays disabled), then enable it when ready:'
        Write-Host '  Enable-ScheduledTask -TaskName "OpenChamber Voice"'
        exit 2
    }

    $taskDir = [string]@($task.Actions)[0].WorkingDirectory
    if ($taskDir -and ($taskDir.TrimEnd('\') -ne $PSScriptRoot.TrimEnd('\'))) {
        Write-Host "Note: the installed task runs the copy in '$taskDir', not this folder." -ForegroundColor DarkGray
    }

    if ($task.State -eq 'Disabled') {
        Write-Host "Task '$name' is disabled. Enable it only when ready:" -ForegroundColor Yellow
        Write-Host '  Enable-ScheduledTask -TaskName "OpenChamber Voice"'
        exit 3
    }

    if ($task.State -eq 'Running') {
        [System.IO.File]::WriteAllText((Join-Path $PSScriptRoot 'show.request'), 'show')
        Write-Host "Worker is already running; asked it to show its window."
        exit 0
    }

    Start-ScheduledTask -TaskName $name -TaskPath $path
    for ($try = 0; $try -lt 16; $try++) {
        Start-Sleep -Milliseconds 250
        if ((Get-ScheduledTask -TaskName $name -TaskPath $path).State -eq 'Running') {
            Write-Host "Worker started by Task Scheduler."
            Write-Host "Logs: $PSScriptRoot\voice.log, guard.log, crash.log"
            exit 0
        }
    }

    $result = (Get-ScheduledTask -TaskName $name -TaskPath $path | Get-ScheduledTaskInfo).LastTaskResult
    Write-Host "Task did not stay running (last result $result). Usually a missing Vosk model folder" -ForegroundColor Yellow
    Write-Host "or a Python without the dependencies; read guard.log and crash.log in $PSScriptRoot"
    exit 4
} catch {
    Write-Host "Failed to start '$name': $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'If PowerShell still blocks this script, clear the mark and allow your own scripts once:'
    Write-Host '  Get-ChildItem -LiteralPath . -File | Unblock-File'
    Write-Host '  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned'
    exit 1
}
