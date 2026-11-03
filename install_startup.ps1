# Registrerer en planlagt opgave der kører synkroniseringen når du logger ind på Windows
# og når du låser computeren op (fx efter dvale).
# Kør:  powershell -ExecutionPolicy Bypass -File .\install_startup.ps1
# Fjern: Unregister-ScheduledTask -TaskName LudusSync -Confirm:$false

$ErrorActionPreference = "Stop"
$projectDir = $PSScriptRoot
$pythonw = Join-Path $projectDir ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $pythonw)) {
    throw "Fandt ikke $pythonw - opret først venv'en (se README)."
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m ludus_sync sync" -WorkingDirectory $projectDir
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = "PT1M"

# Opening a laptop from sleep is an unlock, not a logon, so also run on unlock.
$unlockClass = Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler -ClassName MSFT_TaskSessionStateChangeTrigger
$unlock = New-CimInstance -CimClass $unlockClass -ClientOnly
$unlock.StateChange = 8  # TASK_SESSION_UNLOCK
$unlock.UserId = "$env:USERDOMAIN\$env:USERNAME"
$unlock.Delay = "PT30S"
$unlock.Enabled = $true
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName "LudusSync" -Action $action -Trigger @($logon, $unlock) -Settings $settings `
    -Description "Synkroniserer Ludus-skema til Google Kalender" -Force | Out-Null

Write-Host "Opgaven 'LudusSync' er oprettet og kører ved login og når computeren låses op."
