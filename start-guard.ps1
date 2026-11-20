# Show an existing worker or ask Task Scheduler to start the supervisor.
$ErrorActionPreference = 'Stop'
$name = 'OpenChamber Voice'
$task = Get-ScheduledTask -TaskName $name -TaskPath '\'
if ($task.State -eq 'Disabled') { throw 'Task is disabled. Enable it only when ready.' }
if ($task.State -eq 'Running') {
    [System.IO.File]::WriteAllText((Join-Path $PSScriptRoot 'show.request'), 'show')
} else {
    Start-ScheduledTask -TaskName $name -TaskPath '\'
}
