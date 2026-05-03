#!/bin/bash
# =============================================================================
# deploy.sh — Setup inicial de Alpha Agent en Google Cloud Run Jobs (free tier)
#
# REQUISITOS:
#   - gcloud CLI instalado: https://cloud.google.com/sdk/docs/install
#   - Docker instalado y corriendo
#   - Cuenta Google Cloud con billing habilitado (no cobra dentro del free tier)
#
# USO (desde D:\Agente):
#   bash gcloud/deploy.sh
# =============================================================================
set -e

PROJECT_ID="alpha-agent-trading"   # Cambiar si está en uso: alpha-agent-2025, etc.
REGION="us-central1"
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/alpha/agent:latest"
SA_NAME="alpha-runner"
SA_EMAIL="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"

echo "======================================================"
echo " Alpha Agent — Google Cloud Run Setup"
echo " Proyecto: $PROJECT_ID | Región: $REGION"
echo "======================================================"

# ── 1. Proyecto y APIs ────────────────────────────────────
echo "[1/7] Creando proyecto y habilitando APIs..."
gcloud projects create "$PROJECT_ID" --quiet 2>/dev/null || echo "  Proyecto ya existe."
gcloud config set project "$PROJECT_ID"

gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  --quiet

# ── 2. Artifact Registry ──────────────────────────────────
echo "[2/7] Creando Artifact Registry..."
gcloud artifacts repositories create alpha \
  --repository-format=docker \
  --location="$REGION" \
  --quiet 2>/dev/null || echo "  Registry ya existe."

gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet

# ── 3. Build y push de imagen ─────────────────────────────
echo "[3/7] Construyendo imagen Docker..."
cd "$(dirname "$0")/.."
docker build -t "$IMAGE" .
docker push "$IMAGE"
echo "  Imagen: $IMAGE"

# ── 4. Service Account ────────────────────────────────────
echo "[4/7] Creando service account..."
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="Alpha Agent Runner" \
  --quiet 2>/dev/null || echo "  SA ya existe."

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/secretmanager.secretAccessor" \
  --quiet
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/run.invoker" \
  --quiet

# ── 5. Secrets ────────────────────────────────────────────
echo "[5/7] Cargando secrets en Secret Manager..."
echo "Ingresá los valores cuando se pidan (Enter para saltear opcionales):"

load_secret() {
  local name=$1
  local required=${2:-false}
  printf "  %s: " "$name"
  read -r val
  if [ -n "$val" ]; then
    echo -n "$val" | gcloud secrets create "$name" --data-file=- --quiet 2>/dev/null || \
    echo -n "$val" | gcloud secrets versions add "$name" --data-file=- --quiet
    echo "    ✓ $name guardado"
  elif [ "$required" = "true" ]; then
    echo "    ERROR: $name es obligatorio"
    exit 1
  fi
}

load_secret "ALPACA_API_KEY"    true
load_secret "ALPACA_SECRET_KEY" true
load_secret "ANTHROPIC_API_KEY" true
load_secret "TWILIO_SID"        true
load_secret "TWILIO_TOKEN"      true
load_secret "MY_PHONE_NUMBER"   true
load_secret "GH_TOKEN"          true   # GitHub PAT con permisos 'repo'
load_secret "ALPACA_DT_API_KEY"
load_secret "ALPACA_DT_SECRET_KEY"
load_secret "GOOGLE_API_KEY"

# ── 6. Cloud Run Jobs ─────────────────────────────────────
echo "[6/7] Creando Cloud Run Jobs..."

SECRETS_FLAG="ALPACA_API_KEY=ALPACA_API_KEY:latest,ALPACA_SECRET_KEY=ALPACA_SECRET_KEY:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,TWILIO_SID=TWILIO_SID:latest,TWILIO_TOKEN=TWILIO_TOKEN:latest,MY_PHONE_NUMBER=MY_PHONE_NUMBER:latest,GH_TOKEN=GH_TOKEN:latest"

for job in alpha-daily alpha-monitor alpha-weekly; do
  task="${job#alpha-}"
  gcloud run jobs create "$job" \
    --image "$IMAGE" \
    --region "$REGION" \
    --service-account "$SA_EMAIL" \
    --set-env-vars "TASK=$task" \
    --set-secrets "$SECRETS_FLAG" \
    --memory 1Gi \
    --cpu 1 \
    --max-retries 1 \
    --task-timeout 1800 \
    --quiet 2>/dev/null || \
  gcloud run jobs update "$job" \
    --image "$IMAGE" \
    --region "$REGION" \
    --set-env-vars "TASK=$task" \
    --set-secrets "$SECRETS_FLAG" \
    --memory 1Gi \
    --cpu 1 \
    --task-timeout 1800 \
    --quiet
  echo "  ✓ $job"
done

# ── 7. Cloud Scheduler ────────────────────────────────────
echo "[7/7] Creando Cloud Scheduler..."

create_scheduler() {
  local name=$1
  local schedule=$2
  local job=$3
  gcloud scheduler jobs create http "$name" \
    --schedule "$schedule" \
    --time-zone "America/Argentina/Buenos_Aires" \
    --location "$REGION" \
    --uri "https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/$job:run" \
    --message-body '{}' \
    --http-method POST \
    --oauth-service-account-email "$SA_EMAIL" \
    --quiet 2>/dev/null || \
  gcloud scheduler jobs update http "$name" \
    --schedule "$schedule" \
    --location "$REGION" \
    --quiet
  echo "  ✓ $name ($schedule ART)"
}

create_scheduler "sched-alpha-daily"   "40 10 * * 1-5"  "alpha-daily"
create_scheduler "sched-alpha-monitor-1" "5 11-16 * * 1-5" "alpha-monitor"
create_scheduler "sched-alpha-monitor-2" "35 11-15 * * 1-5" "alpha-monitor"
create_scheduler "sched-alpha-weekly"  "30 15 * * 5"    "alpha-weekly"

echo ""
echo "======================================================"
echo " Setup completado!"
echo "======================================================"
echo ""
echo " TEST manual:"
echo "   gcloud run jobs execute alpha-daily --region $REGION"
echo ""
echo " Ver logs:"
echo "   gcloud run jobs executions list --job alpha-daily --region $REGION"
echo "   gcloud logging read 'resource.type=cloud_run_job' --limit=50"
echo "======================================================"
