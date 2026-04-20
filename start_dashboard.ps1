# ===========================================================================
# start_dashboard.ps1 — arranca Flask + ngrok en background, sin interaccion
# Llamado automaticamente por Task Scheduler a las 09:55 ART
# ===========================================================================

$ErrorActionPreference = "Continue"
Set-Location "D:\Agente"
$env:PYTHONIOENCODING = "utf-8"

$logFile = "D:\Agente\logs\dashboard_$(Get-Date -Format 'yyyy-MM-dd').log"

function Log($msg) {
    $line = "[$(Get-Date -Format 'HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

# Matar instancias anteriores del dashboard si las hay
Get-Process python -ErrorAction SilentlyContinue | Where-Object {
    $_.MainWindowTitle -like "*dashboard*" -or $_.CommandLine -like "*dashboard/app*"
} | Stop-Process -Force -ErrorAction SilentlyContinue

# Actualizar DuckDNS con IP publica actual
Log "Actualizando DuckDNS..."
python "D:\Agente\update_duckdns.py" 2>&1 | ForEach-Object { Log $_ }

Log "Iniciando Alpha Dashboard..."

# Matar ngrok anterior si quedó colgado
Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Start-Process python `
    -ArgumentList "dashboard/app.py" `
    -WorkingDirectory "D:\Agente" `
    -WindowStyle Hidden `
    -RedirectStandardOutput "D:\Agente\logs\dashboard_stdout.log" `
    -RedirectStandardError  "D:\Agente\logs\dashboard_stderr.log"

Start-Sleep -Seconds 5

# Verificar que levanto
try {
    $r = Invoke-WebRequest -Uri "http://localhost:5050/" -TimeoutSec 5 -UseBasicParsing
    Log "Dashboard OK (HTTP $($r.StatusCode))"
} catch {
    Log "WARNING: Dashboard no responde todavia"
}

# Arrancar ngrok con dominio estatico
$domain = python -c "from dotenv import load_dotenv; load_dotenv(); import os; print(os.getenv('NGROK_DOMAIN',''))" 2>$null
if ($domain) {
    Start-Process "D:\Agente\ngrok.exe" `
        -ArgumentList "http --url $domain 5050" `
        -RedirectStandardError "D:\Agente\logs\ngrok.log" `
        -WindowStyle Hidden
    Start-Sleep -Seconds 8
    $tunnelOk = (Invoke-WebRequest "http://localhost:4040/api/tunnels" -UseBasicParsing -EA SilentlyContinue).Content
    if ($tunnelOk -match "public_url") {
        Log "Tunel ngrok activo: https://$domain"
        Log "Webhook WhatsApp: https://$domain/webhook/whatsapp"
    } else {
        Log "WARNING: ngrok no pudo iniciar — revisa logs/ngrok.log"
    }
} else {
    Log "WARNING: NGROK_DOMAIN no configurado en .env"
}

Log "Dashboard: http://localhost:5050 | Publico: https://$domain"
