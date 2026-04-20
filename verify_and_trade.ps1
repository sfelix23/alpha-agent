# ===========================================================================
# verify_and_trade.ps1 -- checklist de verificacion antes de operar en Alpaca
#
# Uso (desde PowerShell de VS Code en D:\Agente):
#     .\verify_and_trade.ps1          -> pipeline completo en DRY-RUN
#     .\verify_and_trade.ps1 -Live    -> ejecuta ordenes reales en Alpaca paper
#     .\verify_and_trade.ps1 -Only analyst   -> solo el analyst
#     .\verify_and_trade.ps1 -Only trader    -> solo el trader
#
# Todo ASCII a proposito: PowerShell 5.1 lee sin BOM como ANSI y los
# caracteres Unicode (cajas, ticks) rompen el parser.
# ===========================================================================

param(
    [switch]$Live,
    [switch]$SendWhatsApp,
    [ValidateSet("all","deps","analyst","backtest","trader")]
    [string]$Only = "all"
)

$ErrorActionPreference = "Stop"
# PowerShell 7+ convierte stderr de comandos nativos en excepcion terminante
# cuando ErrorActionPreference=Stop. Lo desactivamos porque chequeamos
# $LASTEXITCODE manualmente en cada paso.
$PSNativeCommandUseErrorActionPreference = $false

function Write-Step($msg) {
    Write-Host ""
    Write-Host "-----------------------------------------------------------" -ForegroundColor DarkGray
    Write-Host " $msg" -ForegroundColor Cyan
    Write-Host "-----------------------------------------------------------" -ForegroundColor DarkGray
}

# -- 1. Dependencias --------------------------------------------------------
if ($Only -eq "all" -or $Only -eq "deps") {
    Write-Step "1. Verificando dependencias Python"
    python -c "import sys; print(' Python', sys.version.split()[0])"

    $need = @("yfinance","pandas","numpy","scipy","python-dotenv","feedparser","twilio","alpaca-py","google-generativeai")
    foreach ($pkg in $need) {
        $mod = $pkg -replace "-","_"
        if ($pkg -eq "alpaca-py") { $mod = "alpaca" }
        if ($pkg -eq "python-dotenv") { $mod = "dotenv" }
        if ($pkg -eq "google-generativeai") { $mod = "google.generativeai" }

        # Usamos cmd /c para aislar stderr del subproceso Python y que
        # PowerShell no lo interprete como error terminante. El exit code
        # se propaga igual a $LASTEXITCODE.
        cmd /c "python -c ""import $mod"" >nul 2>nul"
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  OK  $pkg" -ForegroundColor Green
        } else {
            Write-Host "  >>  instalando $pkg" -ForegroundColor Yellow
            # --no-cache-dir evita MAX_PATH en Python de Microsoft Store
            # (el cache de wheels genera paths > 260 chars y rompe sgmllib3k)
            pip install --no-cache-dir $pkg
        }
    }

    Write-Host "  Actualizando alpaca-py (soporte opciones)..." -ForegroundColor Yellow
    pip install --no-cache-dir -U alpaca-py | Out-Null
}

# -- 2. Analyst -------------------------------------------------------------
if ($Only -eq "all" -or $Only -eq "analyst") {
    Write-Step "2. Corriendo analyst (genera signals/latest.json)"
    if ($SendWhatsApp) {
        Write-Host "  (enviando reporte a WhatsApp al finalizar)" -ForegroundColor Yellow
        python run_analyst.py --send --no-ai
    } else {
        python run_analyst.py --no-send --no-ai
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: analyst fallo. Revisa el stacktrace arriba." -ForegroundColor Red
        exit 1
    }

    Write-Host ""
    Write-Host "Resumen de signals/latest.json:" -ForegroundColor Cyan
    python inspect_signals.py
}

# -- 3. Backtest ------------------------------------------------------------
if ($Only -eq "all" -or $Only -eq "backtest") {
    Write-Step "3. Backtest walk-forward (validacion out-of-sample)"
    python run_backtest.py --lookback 252 --rebalance 21 --cost-bps 10
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: backtest fallo. No operes hasta diagnosticar." -ForegroundColor Red
        exit 1
    }
}

# -- 4. Trader --------------------------------------------------------------
if ($Only -eq "all" -or $Only -eq "trader") {
    if ($Live) {
        Write-Step "4. Trader en modo LIVE (envia ordenes a Alpaca paper)"
        Write-Host "  !! Solo ejecuta si el mercado NYSE esta abierto." -ForegroundColor Yellow
        python run_trader.py --live
    } else {
        Write-Step "4. Trader en DRY-RUN (imprime ordenes sin enviar)"
        python run_trader.py
    }
}

Write-Host ""
Write-Host "===========================================================" -ForegroundColor Green
Write-Host " OK - Pipeline completo" -ForegroundColor Green
Write-Host "===========================================================" -ForegroundColor Green
