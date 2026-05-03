# deploy.ps1 — Setup completo de Alpha Agent en Google Cloud Run Jobs
# Ejecutar desde D:\Agente en PowerShell (no requiere Admin)
# Prerequisito: gcloud CLI instalado y autenticado (gcloud auth login)
$P      = "alpha-agent-2025"
$REGION = "us-central1"
$IMAGE  = "$REGION-docker.pkg.dev/$P/alpha/agent:latest"
$SA     = "alpha-runner@$P.iam.gserviceaccount.com"

# 1. Proyecto y APIs
gcloud config set project $P
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com --project $P --quiet

# 2. Artifact Registry
gcloud artifacts repositories create alpha --repository-format=docker --location=$REGION --project $P --quiet 2>$null

# 3. Build (sin Docker local)
gcloud builds submit --tag $IMAGE --project $P .

# 4. Service Account
gcloud iam service-accounts create alpha-runner --display-name="Alpha Runner" --project $P --quiet 2>$null
gcloud projects add-iam-policy-binding $P --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor" --quiet
gcloud projects add-iam-policy-binding $P --member="serviceAccount:$SA" --role="roles/run.invoker" --quiet

# 5. Secrets (leer desde .env)
.\gcloud\create_secrets.ps1

# 6. Cloud Run Jobs
$SECRETS = "ALPACA_API_KEY=ALPACA_API_KEY:latest,ALPACA_SECRET_KEY=ALPACA_SECRET_KEY:latest,ALPACA_DT_API_KEY=ALPACA_DT_API_KEY:latest,ALPACA_DT_SECRET_KEY=ALPACA_DT_SECRET_KEY:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,TWILIO_SID=TWILIO_SID:latest,TWILIO_TOKEN=TWILIO_TOKEN:latest,MY_PHONE_NUMBER=MY_PHONE_NUMBER:latest,GH_TOKEN=GH_TOKEN:latest,GOOGLE_API_KEY=GOOGLE_API_KEY:latest"

foreach ($task in @("daily","monitor","weekly")) {
    gcloud run jobs create "alpha-$task" --image $IMAGE --region $REGION --project $P `
        --service-account $SA --set-env-vars "TASK=$task" --set-secrets $SECRETS `
        --memory 1Gi --cpu 1 --max-retries 1 --task-timeout 1800 --quiet 2>$null
    Write-Host "OK: alpha-$task"
}

# 7. Cloud Scheduler
$schedules = @(
    @{name="sched-daily";   cron="40 10 * * 1-5";      job="alpha-daily"},
    @{name="sched-monitor"; cron="5,35 11-15 * * 1-5"; job="alpha-monitor"},
    @{name="sched-weekly";  cron="30 15 * * 5";        job="alpha-weekly"}
)
foreach ($s in $schedules) {
    $uri = "https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$P/jobs/$($s.job):run"
    gcloud scheduler jobs create http $s.name --schedule $s.cron `
        --time-zone "America/Argentina/Buenos_Aires" --location $REGION --project $P `
        --uri $uri --message-body "{}" --http-method POST `
        --oauth-service-account-email $SA --quiet 2>$null
    Write-Host "OK scheduler: $($s.name)"
}

Write-Host "`nListo. Test: gcloud run jobs execute alpha-daily --region $REGION --project $P"
