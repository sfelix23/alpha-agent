# ===========================================================================
# install_scheduler.ps1 - registra las tareas del agente financiero
# Correr como Administrador
# Para desinstalar: .\install_scheduler.ps1 -Uninstall
# ===========================================================================

param([switch]$Uninstall)

$baseDir = "D:\Agente"
$taskNames = @("Alpha Wake", "Alpha Dashboard", "Alpha PreMarket", "Alpha Analyst", "Alpha Monitor", "Alpha Midday", "Alpha DayTrader", "Alpha Rebalancer", "Alpha Health", "Alpha Portfolio Review", "Alpha Email Digest")

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
# TAREA 0b: Alpha PreMarket - 09:00 (gap scanner antes de la apertura)
# =========================================================
$aPM = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -Command `"cd '$baseDir'; python '$baseDir\run_premarket.py' 2>&1`"") `
    -WorkingDirectory $baseDir

$tPM = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "09:00"

Register-ScheduledTask `
    -TaskName "Alpha PreMarket" `
    -Action $aPM `
    -Trigger $tPM `
    -Settings (New-BaseSettings 5) `
    -Principal $principal `
    -Description "Gap scanner pre-market: alerta WhatsApp+Telegram con gaps >2%" `
    -Force | Out-Null

if (Get-ScheduledTask -TaskName "Alpha PreMarket" -ErrorAction SilentlyContinue) {
    Write-Host "  [OK] Alpha PreMarket  - 09:00 ART (gap scanner)" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Alpha PreMarket" -ForegroundColor Red
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
# TAREA 2b: Alpha DayTrader - 11:30 ART (ORB window: 60min post-open)
# La estrategia DT necesita que el Opening Range (primeros 30 min) ya este
# definido antes de entrar. 11:30 ART = 10:30 EDT = 60 min post-apertura.
# Usa la cuenta Alpaca SEPARADA (ALPACA_DT_API_KEY / ALPACA_DT_SECRET_KEY).
# =========================================================
$dtArg = "-NoProfile -ExecutionPolicy Bypass -Command `"cd '$baseDir'; python '$baseDir\run_daytrader.py' --live 2>&1`""

$aDT = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $dtArg `
    -WorkingDirectory $baseDir

$tDT = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "11:30"

Register-ScheduledTask `
    -TaskName "Alpha DayTrader" `
    -Action $aDT `
    -Trigger $tDT `
    -Settings (New-BaseSettings 30) `
    -Principal $principal `
    -Description "Day Trader: gap+ORB+VWAP, 1 posicion concentrada, dual bracket" `
    -Force | Out-Null

if (Get-ScheduledTask -TaskName "Alpha DayTrader" -ErrorAction SilentlyContinue) {
    Write-Host "  [OK] Alpha DayTrader  - 11:30 ART (ORB window)" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Alpha DayTrader" -ForegroundColor Red
}

# =========================================================
# TAREA 3: Alpha Monitor - cada 15 min de 10:50 a 17:05
# Cubre todo el horario NYSE (10:30-17:00 ART) con margen.
# El monitor sale solo si el mercado está cerrado.
# =========================================================
$monitorArg = "-NoProfile -ExecutionPolicy Bypass -Command `"cd '$baseDir'; python '$baseDir\run_monitor.py' --live 2>&1`""

$a3 = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $monitorArg `
    -WorkingDirectory $baseDir

$triggers3 = @()
$startTotalMin = 10 * 60 + 50   # 10:50 ART
for ($i = 0; $i -lt 26; $i++) { # 26 × 15 min = 375 min → hasta 17:05 ART
    $totalMin = $startTotalMin + ($i * 15)
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
    -Description "Monitor cada 15 min de 10:50 a 17:05 (stops, TPs, trailing, kill switch)" `
    -Force | Out-Null

if (Get-ScheduledTask -TaskName "Alpha Monitor" -ErrorAction SilentlyContinue) {
    Write-Host "  [OK] Alpha Monitor    - cada 15min de 10:50 a 17:05" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Alpha Monitor" -ForegroundColor Red
}

# =========================================================
# TAREA 3b: Alpha Midday - 14:00 (segundo ciclo CP)
# Escaneo técnico rápido (~3 min) sin CAPM/Markowitz.
# Solo abre posiciones CP nuevas si hay capital disponible.
# =========================================================
$middayArg = "-NoProfile -ExecutionPolicy Bypass -Command `"cd '$baseDir'; python '$baseDir\run_midday.py' --live 2>&1`""

$aMD = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $middayArg `
    -WorkingDirectory $baseDir

$tMD = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "14:00"

Register-ScheduledTask `
    -TaskName "Alpha Midday" `
    -Action $aMD `
    -Trigger $tMD `
    -Settings (New-BaseSettings 10) `
    -Principal $principal `
    -Description "Midday CP scan: scoring técnico rápido, abre slots CP disponibles" `
    -Force | Out-Null

if (Get-ScheduledTask -TaskName "Alpha Midday" -ErrorAction SilentlyContinue) {
    Write-Host "  [OK] Alpha Midday     - 14:00 ART (CP scan)" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Alpha Midday" -ForegroundColor Red
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

# =========================================================
# TAREA 5: Alpha Health - lun-vie 12:30
# =========================================================
$a5 = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -Command `"cd '$baseDir'; python '$baseDir\run_health_check.py' 2>&1`"") `
    -WorkingDirectory $baseDir

$t5 = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "12:30"

Register-ScheduledTask `
    -TaskName "Alpha Health" `
    -Action $a5 `
    -Trigger $t5 `
    -Settings (New-BaseSettings 5) `
    -Principal $principal `
    -Description "Health check: alerta si el bot no corrió" `
    -Force | Out-Null

if (Get-ScheduledTask -TaskName "Alpha Health" -ErrorAction SilentlyContinue) {
    Write-Host "  [OK] Alpha Health     - 12:30 ART lun-vie" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Alpha Health" -ForegroundColor Red
}

# =========================================================
# TAREA 6: Alpha Portfolio Review - domingos 20:00
# =========================================================
$a6 = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -Command `"cd '$baseDir'; python '$baseDir\run_portfolio_review.py' 2>&1`"") `
    -WorkingDirectory $baseDir

$t6 = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Sunday `
    -At "20:00"

Register-ScheduledTask `
    -TaskName "Alpha Portfolio Review" `
    -Action $a6 `
    -Trigger $t6 `
    -Settings (New-BaseSettings 10) `
    -Principal $principal `
    -Description "Revisión semanal de portfolio con Claude Sonnet" `
    -Force | Out-Null

if (Get-ScheduledTask -TaskName "Alpha Portfolio Review" -ErrorAction SilentlyContinue) {
    Write-Host "  [OK] Alpha Portfolio Review - domingos 20:00" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Alpha Portfolio Review" -ForegroundColor Red
}

# =========================================================
# TAREA 7: Alpha Email Digest - viernes 17:00
# =========================================================
$a7 = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -Command `"cd '$baseDir'; python '$baseDir\run_performance.py' 2>&1`"") `
    -WorkingDirectory $baseDir

$t7 = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Friday `
    -At "17:00"

Register-ScheduledTask `
    -TaskName "Alpha Email Digest" `
    -Action $a7 `
    -Trigger $t7 `
    -Settings (New-BaseSettings 10) `
    -Principal $principal `
    -Description "Reporte semanal: P&L, Sharpe, win rate, alpha vs SPY" `
    -Force | Out-Null

if (Get-ScheduledTask -TaskName "Alpha Email Digest" -ErrorAction SilentlyContinue) {
    Write-Host "  [OK] Alpha Performance  - viernes 17:00" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Alpha Performance" -ForegroundColor Red
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
Write-Host "  09:00  Pre-market gap scanner (WhatsApp+Telegram)"
Write-Host "  09:55  Dashboard Flask + ngrok"
Write-Host "  10:00  PC despierta de Sleep"
Write-Host "  10:35  Analyst + Trader + WhatsApp"
Write-Host "  10:50  Monitor cada 15 min hasta 17:05 (cobertura completa)"
Write-Host "  11:30  DayTrader (ORB window, cuenta separada)"
Write-Host "  12:30  Health check (alerta si bot no corrió)"
Write-Host "  14:00  Midday scan CP (nuevo slot si hay capital)"
Write-Host "  15:00  Rebalancer semanal (viernes)"
Write-Host "  17:00  Email digest semanal (viernes)"
Write-Host "  17:15  PC a dormir"
Write-Host "  20:00  Portfolio review con Claude (domingos)"
Write-Host ""
Write-Host "ANTES DE IR A LA FACULTAD:" -ForegroundColor Yellow
Write-Host "  Win+X > Suspender (NO apagar)"
