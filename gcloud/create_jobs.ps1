$P      = "alpha-agent-2025"
$REGION = "us-central1"
$IMAGE  = "$REGION-docker.pkg.dev/$P/alpha/agent:latest"
$SA     = "alpha-runner@$P.iam.gserviceaccount.com"

$SECRETS = "ALPACA_API_KEY=ALPACA_API_KEY:latest,ALPACA_SECRET_KEY=ALPACA_SECRET_KEY:latest,ALPACA_DT_API_KEY=ALPACA_DT_API_KEY:latest,ALPACA_DT_SECRET_KEY=ALPACA_DT_SECRET_KEY:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,TWILIO_SID=TWILIO_SID:latest,TWILIO_TOKEN=TWILIO_TOKEN:latest,MY_PHONE_NUMBER=MY_PHONE_NUMBER:latest,GH_TOKEN=GH_TOKEN:latest,GOOGLE_API_KEY=GOOGLE_API_KEY:latest"

foreach ($task in @("daily","monitor","weekly")) {
    $job = "alpha-$task"
    gcloud run jobs create $job `
        --image $IMAGE --region $REGION --project $P `
        --service-account $SA `
        --set-env-vars "TASK=$task" `
        --set-secrets $SECRETS `
        --memory 1Gi --cpu 1 `
        --max-retries 1 --task-timeout 1800 --quiet 2>$null
    if (-not $?) {
        gcloud run jobs update $job `
            --image $IMAGE --region $REGION --project $P `
            --set-env-vars "TASK=$task" `
            --set-secrets $SECRETS `
            --memory 1Gi --cpu 1 --task-timeout 1800 --quiet
    }
    Write-Host "OK: $job"
}

# Cloud Scheduler
$schedules = @(
    @{name="sched-daily";   cron="40 10 * * 1-5";    job="alpha-daily"},
    @{name="sched-monitor"; cron="5,35 11-15 * * 1-5"; job="alpha-monitor"},
    @{name="sched-weekly";  cron="30 15 * * 5";      job="alpha-weekly"}
)

foreach ($s in $schedules) {
    $uri = "https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$P/jobs/$($s.job):run"
    gcloud scheduler jobs create http $s.name `
        --schedule $s.cron `
        --time-zone "America/Argentina/Buenos_Aires" `
        --location $REGION --project $P `
        --uri $uri --message-body "{}" --http-method POST `
        --oauth-service-account-email $SA --quiet 2>$null
    Write-Host "OK scheduler: $($s.name)"
}

Write-Host "`nTodo listo. Test:"
Write-Host "  gcloud run jobs execute alpha-daily --region $REGION --project $P"
