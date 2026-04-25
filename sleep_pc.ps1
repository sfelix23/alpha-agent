# sleep_pc.ps1 — Suspende la PC inmediatamente
# Uso: doble click, o desde terminal: .\sleep_pc.ps1
#
# Mata el loop de market_wake.ps1 si está corriendo,
# manda WhatsApp de confirmación, y suspende el sistema.

$ErrorActionPreference = "Continue"
Set-Location -Path "D:\Agente"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Preparando para suspender PC..."

# Avisar por WhatsApp
try {
    python -c @"
from dotenv import load_dotenv; load_dotenv()
from alpha_agent.notifications import send_whatsapp
send_whatsapp('💤 *PC entrando en reposo*\nSesion de trading terminada. Proxima sesion: manana 10:00 ART.')
"@ 2>&1 | Out-Null
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] WhatsApp enviado."
} catch {
    Write-Host "WhatsApp error: $_"
}

Start-Sleep -Seconds 5

# Suspender (Sleep, no Shutdown — Task Scheduler puede despertar desde Sleep)
Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Suspendiendo..."
try {
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public class SleepNow {
    [DllImport("powrprof.dll")]
    public static extern bool SetSuspendState(bool hibernate, bool forceCritical, bool disableWakeEvent);
}
"@ -ErrorAction SilentlyContinue
} catch {}

[SleepNow]::SetSuspendState($false, $false, $false)
