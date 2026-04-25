# ===========================================================================
# run_autonomous.ps1 -- pipeline desatendido para Task Scheduler
#
# Este script es lo que ejecuta Windows Task Scheduler cada dia de mercado.
# Corre el pipeline completo sin pedir input, manda resumen a WhatsApp y
# envia las ordenes reales a Alpaca paper.
#
# Loguea todo a D:\Agente\logs\autonomous_YYYY-MM-DD.log
# ===========================================================================

$ErrorActionPreference = "Continue"
$PSNativeCommandUseErrorActionPreference = $false

# Forzar UTF-8 en PowerShell (fix para emojis y caracteres Unicode en logs)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

# Ir a la carpeta del proyecto aunque Task Scheduler arranque desde otro lado
Set-Location -Path "D:\Agente"

# Carpeta de logs
$logDir = "D:\Agente\logs"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}
$logFile = Join-Path $logDir ("autonomous_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

function Log($msg) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$stamp] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

Log "==============================================="
Log "  AUTONOMOUS RUN START"
Log "==============================================="

# Chequeo de dia de semana — Task Scheduler ya filtra esto, pero por las dudas
$dow = (Get-Date).DayOfWeek
if ($dow -eq "Saturday" -or $dow -eq "Sunday") {
    Log "Es fin de semana ($dow). Abortando."
    exit 0
}

# -- 0. Capital dinámico: obtener equity actual de Alpaca (reinversión) --
Log "Paso 0: obteniendo equity actual de Alpaca..."
$equityResult = python -c "
from dotenv import load_dotenv; load_dotenv()
from trader_agent.brokers.alpaca_broker import AlpacaBroker
try:
    b = AlpacaBroker(paper=True)
    print(f'{b.get_equity():.2f}')
except Exception as e:
    print(f'ERROR:{e}')
" 2>&1
$capitalArg = ""
if ($equityResult -match '^\d') {
    $equity = [double]$equityResult
    Log "Equity actual en Alpaca: `$$equity USD (reinversion automatica)"
    $capitalArg = "--capital $equity"
} else {
    Log "No se pudo obtener equity ($equityResult). Usando capital default."
}

# -- 1. Analyst con envio de WhatsApp -----------------------------------
Log "Paso 1/2: analyst + WhatsApp"
$analystCmd = "python run_analyst.py --send --no-ai $capitalArg"
Invoke-Expression "$analystCmd 2>&1" | Tee-Object -FilePath $logFile -Append -Encoding UTF8
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: analyst fallo con exit code $LASTEXITCODE"
    # Intentar avisar por WhatsApp aunque sea el error
    python -c "from alpha_agent.notifications import send_whatsapp; send_whatsapp('ALPHA ERROR: analyst fallo en run autonomo. Revisa logs.')" 2>&1 | Out-Null
    exit 1
}

# -- 2. Trader en modo LIVE ---------------------------------------------
Log "Paso 2/2: trader live (Alpaca paper)"
# Pasamos --capital para que el trader use el mismo equity cap que el analyst.
# Esto evita que use el buying_power 2x de Alpaca margin y sobre-invierta.
$traderCmd = "python run_trader.py --live $capitalArg"
Invoke-Expression "$traderCmd 2>&1" | Tee-Object -FilePath $logFile -Append -Encoding UTF8
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: trader fallo con exit code $LASTEXITCODE"
    python -c "from alpha_agent.notifications import send_whatsapp; send_whatsapp('ALPHA ERROR: trader fallo en run autonomo. Revisa logs.')" 2>&1 | Out-Null
    exit 1
}

Log "==============================================="
Log "  AUTONOMOUS RUN OK"
Log "==============================================="

# Nota: el sleep de la PC lo maneja market_wake.ps1 a las 17:15.
# Este script ya no necesita poner la PC a dormir.
exit 0
