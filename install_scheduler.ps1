# ===========================================================================
# install_scheduler.ps1 - registra las tareas del agente financiero
# Correr como Administrador
# Para desinstalar: .\install_scheduler.ps1 -Uninstall
# ===========================================================================

param([switch]$Uninstall)

$baseDir = "D:\Agente"
$taskNames = @("Alpha Wake", "Alpha Dashboard", "Alpha Analyst", "Alpha Monitor", "Alpha Rebalancer")

# 1. Verificar admin
$me = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: Necesita Administrador." -ForegroundColor Red
    exit 1
}

# 2. Modo desinstalar
if ($Uninstall) {
    foreach ($name in $taskNames) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "  Eliminada: $name" -ForegroundColor Green
        }
    }
    Write-Host "Listo." -ForegroundColor Green
    exit 0
}

# 3. Obtener usuario actual (S4U: sin password, funciona con pantalla bloqueada)
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
Write-Host "Usuario: $userId" -ForegroundColor Cyan
Write-Host "LogonType: S4U (sin password, compatible con PIN y cuenta Microsoft)" -ForegroundColor Cyan

# 4. Eliminar tareas anteriores
Write-Host ""
Write-Host "Limpiando tareas anteriores..." -ForegroundColor Yellow
foreach ($name in $taskNames) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "  Eliminada: $name" -ForegroundColor Gray
    }
}

# Settings base compartidos
function New-BaseSettings {
    param([int]$timeoutMinutes)
    return New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -WakeToRun `
        -ExecutionTimeLimit (New-TimeSpan -Minutes $timeoutMinutes)
}

# Principal S4U compartido para todas las tareas
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType S4U -RunLevel Limited

Write-Host ""

# =========================================================
# TAREA 0: Alpha Dashboard - 09:55 (Flask + ngrok antes del analyst)
# =========================================================
$a0 = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -File `"$baseDir\start_dashboard.ps1`"") `
    -WorkingDirectory $baseDir

$t0 = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "09:55"

Register-ScheduledTask `
    -TaskName "Alpha Dashboard" `
    -Action $a0 `
    -Trigger $t0 `
    -Settings (New-BaseSettings 480) `
    -Principal $principal `
    -Description "Inicia Flask dashboard + ngrok (webhook WhatsApp)" `
    -Force | Out-Null

if (Get-ScheduledTask -TaskName "Alpha Dashboard" -ErrorAction SilentlyContinue) {
    Write-Host "  [OK] Alpha Dashboard  - 09:55 ART (Flask + ngrok)" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Alpha Dashboard" -ForegroundColor Red
}

# =========================================================
# TAREA 1: Alpha Wake - 10:00
# =========================================================
$a1 = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -File `"$baseDir\market_wake.ps1`"") `
    -WorkingDirectory $baseDir

$t1 = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "10:00"

Register-ScheduledTask `
    -TaskName "Alpha Wake" `
    -Action $a1 `
    -Trigger $t1 `
    -Settings (New-BaseSettings 480) `
    -Principal $principal `
    -Description "Despierta la PC a las 10:00" `
    -Force | Out-Null

if (Get-ScheduledTask -TaskName "Alpha Wake" -ErrorAction SilentlyContinue) {
    Write-Host "  [OK] Alpha Wake       - 10:00 ART" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Alpha Wake" -ForegroundColor Red
}

# =========================================================
# TAREA 2: Alpha Analyst - 10:35
# =========================================================
$a2 = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -File `"$baseDir\run_autonomous.ps1`"") `
    -WorkingDirectory $baseDir

$t2 = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "10:35"

Register-ScheduledTask `
    -TaskName "Alpha Analyst" `
    -Action $a2 `
    -Trigger $t2 `
    -Settings (New-BaseSettings 20) `
    -Principal $principal `
    -Description "Pipeline: analyst + trader + WhatsApp" `
    -Force | Out-Null

if (Get-ScheduledTask -TaskName "Alpha Analyst" -ErrorAction SilentlyContinue) {
    Write-Host "  [OK] Alpha Analyst    - 10:35 ART" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Alpha Analyst" -ForegroundColor Red
}

# =========================================================
# TAREA 3: Alpha Monitor - cada 30 min de 11:05 a 16:35
# =========================================================
$monitorArg = "-NoProfile -ExecutionPolicy Bypass -Command `"cd '$baseDir'; python '$baseDir\run_monitor.py' --live 2>&1`""

$a3 = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $monitorArg `
    -WorkingDirectory $baseDir

$triggers3 = @()
$startTotalMin = 11 * 60 + 5
for ($i = 0; $i -lt 13; $i++) {
    $totalMin = $startTotalMin + ($i * 30)
    $h = [int]($totalMin / 60)
    $m = [int]($totalMin % 60)
    $timeStr = $h.ToString("00") + ":" + $m.ToString("00")
    $triggers3 += New-ScheduledTaskTrigger -Weekly `
        -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
        -At $timeStr
}

Register-ScheduledTask `
    -TaskName "Alpha Monitor" `
    -Action $a3 `
    -Trigger $triggers3 `
    -Settings (New-BaseSettings 5) `
    -Principal $principal `
    -Description "Monitor cada 30 min (stops, TPs, trailing, kill switch)" `
    -Force | Out-Null

if (Get-ScheduledTask -TaskName "Alpha Monitor" -ErrorAction SilentlyContinue) {
    Write-Host "  [OK] Alpha Monitor    - cada 30min de 11:05 a 16:35" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Alpha Monitor" -ForegroundColor Red
}

# =========================================================
# TAREA 4: Alpha Rebalancer - viernes 15:00
# =========================================================
$a4 = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -Command `"cd '$baseDir'; python '$baseDir\run_rebalancer.py' --live 2>&1`"") `
    -WorkingDirectory $baseDir

$t4 = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Friday `
    -At "15:00"

Register-ScheduledTask `
    -TaskName "Alpha Rebalancer" `
    -Action $a4 `
    -Trigger $t4 `
    -Settings (New-BaseSettings 10) `
    -Principal $principal `
    -Description "Rebalanceo semanal Markowitz+Kelly" `
    -Force | Out-Null

if (Get-ScheduledTask -TaskName "Alpha Rebalancer" -ErrorAction SilentlyContinue) {
    Write-Host "  [OK] Alpha Rebalancer - viernes 15:00 ART" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Alpha Rebalancer" -ForegroundColor Red
}

# Habilitar wake timers
Write-Host ""
Write-Host "Habilitando wake timers..." -ForegroundColor Cyan
& powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP ALLOWSTANDBYWAKETIMERS 1 2>$null
& powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP ALLOWSTANDBYWAKETIMERS 1 2>$null
& powercfg /SETACTIVE SCHEME_CURRENT 2>$null
Write-Host "  [OK] Wake timers habilitados" -ForegroundColor Green

Write-Host ""
Write-Host "Verificando tareas registradas..." -ForegroundColor Cyan
Get-ScheduledTask | Where-Object { $_.TaskName -like "Alpha*" } | Format-Table TaskName, State -AutoSize

Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host " SISTEMA INSTALADO Y OPERATIVO" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "HORARIO (lunes a viernes):" -ForegroundColor Cyan
Write-Host "  10:00  PC despierta de Sleep"
Write-Host "  10:35  Analyst + Trader + WhatsApp"
Write-Host "  11:05  Monitor cada 30 min hasta 16:35"
Write-Host "  15:00  Rebalancer semanal (viernes)"
Write-Host "  17:15  PC a dormir"
Write-Host ""
Write-Host "ANTES DE IR A LA FACULTAD:" -ForegroundColor Yellow
Write-Host "  Win+X > Suspender (NO apagar)"
