# ===========================================================================
# market_wake.ps1 — mantiene la PC despierta durante horario de mercado
#
# Se ejecuta automaticamente a las 10:00 ART via Task Scheduler (con
# -WakeToRun para despertar la PC de Sleep/Hibernate).
#
# Lo que hace:
#   1. Activa un power plan que impide que Windows vuelva a dormirse
#   2. Mantiene la PC despierta hasta las 17:15 ART (cierre NYSE + buffer)
#   3. A las 17:15 restaura el plan original y pone la PC a dormir
#
# Funciona con o sin usuario presente (sesion bloqueada OK).
# ===========================================================================

$ErrorActionPreference = "Continue"

$logDir  = "D:\Agente\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir ("market_wake_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

function Log($msg) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line  = "[$stamp] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line
}

# --- Chequeo de dia laboral ---
$dow = (Get-Date).DayOfWeek
if ($dow -eq "Saturday" -or $dow -eq "Sunday") {
    Log "Fin de semana ($dow). No hay mercado. Saliendo."
    exit 0
}

Log "============================================="
Log "  MARKET WAKE — manteniendo PC despierta"
Log "============================================="

# --- 1. Prevenir que Windows entre en sleep ---
# Usamos SetThreadExecutionState via P/Invoke
# ES_CONTINUOUS (0x80000000) + ES_SYSTEM_REQUIRED (0x00000001) + ES_DISPLAY_REQUIRED (0x00000002)
try {
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public class PowerKeepAlive {
    [DllImport("kernel32.dll")]
    public static extern uint SetThreadExecutionState(uint esFlags);

    public static void PreventSleep() {
        // ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        SetThreadExecutionState(0x80000001);
    }
    public static void AllowSleep() {
        // ES_CONTINUOUS only → permite sleep de nuevo
        SetThreadExecutionState(0x80000000);
    }
}
"@ -ErrorAction SilentlyContinue
} catch {
    # Si ya se cargo el tipo en esta sesion, ignorar
}

[PowerKeepAlive]::PreventSleep()
Log "Sleep bloqueado. La PC NO se dormira hasta las 17:15."

# --- 2. Loop: mantener viva hasta hora de cierre ---
$marketCloseLocal = (Get-Date).Date.AddHours(17).AddMinutes(0)   # 17:00 ART = 16:00 ET (NYSE close)
Log "Manteniendo hasta: $($marketCloseLocal.ToString('HH:mm'))"

# Heartbeat cada 10 minutos para mantener el flag activo y loguear estado
while ((Get-Date) -lt $marketCloseLocal) {
    [PowerKeepAlive]::PreventSleep()   # refrescar el flag
    $remaining = $marketCloseLocal - (Get-Date)
    $minLeft   = [Math]::Round($remaining.TotalMinutes)
    Log "Heartbeat: PC activa. Faltan $minLeft min para cierre."
    Start-Sleep -Seconds 600   # 10 minutos
}

# --- 3. Fin del dia: liberar y dormir ---
Log "Mercado cerrado. Liberando bloqueo de sleep."
[PowerKeepAlive]::AllowSleep()

# Notificar por WhatsApp que el dia de trading termino
try {
    Set-Location -Path "D:\Agente"
    python -c @"
from dotenv import load_dotenv; load_dotenv()
from alpha_agent.notifications import send_whatsapp
from trader_agent.brokers.alpaca_broker import AlpacaBroker
try:
    b = AlpacaBroker(paper=True)
    equity = b.get_equity()
    positions = b.get_positions()
    n_pos = len(positions)
    pnl_lines = []
    for p in positions[:5]:
        pnl_lines.append(f'  {p.ticker}: {p.qty} @ \${p.avg_price:.2f} → \${p.current_price:.2f} ({p.pnl_pct:+.1f}%)')
    pnl_text = chr(10).join(pnl_lines) if pnl_lines else '  (sin posiciones)'
    msg = f'''🔔 *CIERRE DE MERCADO*
Equity: \${equity:.2f}
Posiciones abiertas: {n_pos}
{pnl_text}

_PC entrando en reposo. Proxima sesion: manana 10:00 ART._'''
    send_whatsapp(msg)
except Exception as e:
    send_whatsapp(f'ALPHA: Cierre de mercado. Error obteniendo equity: {e}')
"@ 2>&1
} catch {
    Log "Error enviando resumen de cierre: $_"
}

# Esperar 60s para que el WhatsApp salga, luego dormir
Log "Suspendiendo PC en 60 segundos..."
Start-Sleep -Seconds 60

# Poner a dormir (Sleep, no Shutdown)
try {
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public class SleepHelper {
    [DllImport("powrprof.dll")]
    public static extern bool SetSuspendState(bool hibernate, bool forceCritical, bool disableWakeEvent);
}
"@ -ErrorAction SilentlyContinue
} catch { }

Log "Entrando en Sleep..."
[SleepHelper]::SetSuspendState($false, $false, $false)
