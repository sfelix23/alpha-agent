# ===========================================================================
# setup_ngrok.ps1 — configuracion ONE-TIME de ngrok con dominio estatico
#
# Pasos:
#   1. Crear cuenta gratis en https://ngrok.com
#   2. Ir a https://dashboard.ngrok.com/get-started/your-authtoken y copiar el token
#   3. Ir a https://dashboard.ngrok.com/domains y copiar tu dominio estatico gratuito
#      (tiene formato algo-algo-algo.ngrok-free.app)
#   4. Correr este script: .\setup_ngrok.ps1
#
# Despues de esto, el sistema es 100% autonomo.
# ===========================================================================

Set-Location "D:\Agente"

Write-Host ""
Write-Host "=== SETUP NGROK (una sola vez) ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Necesito dos datos de tu cuenta ngrok.com (es gratis):" -ForegroundColor Yellow
Write-Host "  1. Tu authtoken: https://dashboard.ngrok.com/get-started/your-authtoken"
Write-Host "  2. Tu dominio estatico: https://dashboard.ngrok.com/domains"
Write-Host ""

$token  = Read-Host "Pega tu ngrok authtoken"
$domain = Read-Host "Pega tu dominio estatico (ej: algo-algo.ngrok-free.app)"

if (-not $token -or -not $domain) {
    Write-Host "ERROR: Faltan datos." -ForegroundColor Red
    exit 1
}

# Guardar authtoken en ngrok
python -c "from pyngrok import ngrok; ngrok.set_auth_token('$token')" 2>&1
Write-Host "  [OK] Authtoken guardado" -ForegroundColor Green

# Guardar en .env
$envPath = "D:\Agente\.env"
$envContent = Get-Content $envPath -Raw -ErrorAction SilentlyContinue
if ($envContent -match "NGROK_AUTHTOKEN") {
    $envContent = $envContent -replace "NGROK_AUTHTOKEN=.*", "NGROK_AUTHTOKEN=$token"
} else {
    $envContent += "`nNGROK_AUTHTOKEN=$token"
}
if ($envContent -match "NGROK_DOMAIN") {
    $envContent = $envContent -replace "NGROK_DOMAIN=.*", "NGROK_DOMAIN=$domain"
} else {
    $envContent += "`nNGROK_DOMAIN=$domain"
}
Set-Content $envPath $envContent -Encoding UTF8
Write-Host "  [OK] Credenciales guardadas en .env" -ForegroundColor Green

$webhookUrl = "https://$domain/webhook/whatsapp"

Write-Host ""
Write-Host "=========================================" -ForegroundColor Green
Write-Host " SETUP COMPLETO" -ForegroundColor Green
Write-Host "=========================================" -ForegroundColor Green
Write-Host ""
Write-Host " Dashboard permanente: https://$domain" -ForegroundColor Cyan
Write-Host ""
Write-Host " PASO FINAL — Configurar Twilio (una sola vez):" -ForegroundColor Yellow
Write-Host " 1. Ir a: https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn"
Write-Host " 2. En 'WHEN A MESSAGE COMES IN' pegar:"
Write-Host "    $webhookUrl" -ForegroundColor Cyan
Write-Host " 3. Guardar. Listo para siempre."
Write-Host ""
Write-Host " De ahora en adelante el sistema opera completamente solo." -ForegroundColor Green
