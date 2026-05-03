# enable_wake.ps1 — Activa wake-from-sleep para las tareas Alpha
# EJECUTAR COMO ADMINISTRADOR: clic derecho → "Ejecutar como administrador"

$me = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: Ejecutar como Administrador." -ForegroundColor Red
    Read-Host "Presioná Enter para salir"
    exit 1
}

Write-Host "Habilitando wake timers en el plan de energia..." -ForegroundColor Cyan
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP HIBERNATEIDLE 0   # desactivar hibernate
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
powercfg /setactive SCHEME_CURRENT
Write-Host "  Wake timers: ON" -ForegroundColor Green
Write-Host "  Hibernacion: OFF" -ForegroundColor Green

$tasks = @("Alpha Wake", "Alpha Analyst", "Alpha Monitor", "Alpha Midday", "Alpha Health", "Alpha Dashboard", "Alpha Rebalancer")

Write-Host ""
Write-Host "Activando WakeToRun en tareas..." -ForegroundColor Cyan
foreach ($name in $tasks) {
    try {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
        $settings = $task.Settings
        $settings.WakeToRun = $true
        Set-ScheduledTask -TaskName $name -Settings $settings | Out-Null
        Write-Host "  OK: $name" -ForegroundColor Green
    } catch {
        Write-Host "  SKIP: $name — $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Listo. La PC se despertara automaticamente para correr las tareas." -ForegroundColor Green
Write-Host ""
Write-Host "IMPORTANTE: La PC debe estar enchufada (no en bateria) para que funcione el wake." -ForegroundColor Yellow
Read-Host "Presiona Enter para cerrar"
