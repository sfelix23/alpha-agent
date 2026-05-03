# create_secrets.ps1 — Cargar secrets desde .env a Google Secret Manager
# Uso: .\gcloud\create_secrets.ps1
$P = "alpha-agent-2025"

# Leer valores desde .env local
$envPath = Join-Path (Split-Path $PSScriptRoot) ".env"
$envVars = @{}
Get-Content $envPath | Where-Object { $_ -match "^[^#].*=.+" } | ForEach-Object {
    $parts = $_ -split "=", 2
    $envVars[$parts[0].Trim()] = $parts[1].Trim()
}

$keys = @(
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
    "ALPACA_DT_API_KEY", "ALPACA_DT_SECRET_KEY",
    "ANTHROPIC_API_KEY",
    "TWILIO_SID", "TWILIO_TOKEN", "MY_PHONE_NUMBER",
    "GH_TOKEN", "GOOGLE_API_KEY"
)

foreach ($k in $keys) {
    $val = $envVars[$k]
    if (-not $val) { Write-Host "SKIP (no encontrado en .env): $k"; continue }
    $tmpFile = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllText($tmpFile, $val, [System.Text.UTF8Encoding]::new($false))
    gcloud secrets create $k --data-file=$tmpFile --project $P --quiet 2>$null
    if (-not $?) { gcloud secrets versions add $k --data-file=$tmpFile --project $P --quiet 2>$null }
    Remove-Item $tmpFile -Force
    Write-Host "OK: $k"
}

Write-Host "`nSecretos listos."
