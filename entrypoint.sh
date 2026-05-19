#!/bin/bash
set -e

# Reconstruct .env from secrets injected by Cloud Run
{
  [ -n "$ALPACA_API_KEY" ]       && echo "ALPACA_API_KEY=$ALPACA_API_KEY"
  [ -n "$ALPACA_SECRET_KEY" ]    && echo "ALPACA_SECRET_KEY=$ALPACA_SECRET_KEY"
  [ -n "$ALPACA_DT_API_KEY" ]    && echo "ALPACA_DT_API_KEY=$ALPACA_DT_API_KEY"
  [ -n "$ALPACA_DT_SECRET_KEY" ] && echo "ALPACA_DT_SECRET_KEY=$ALPACA_DT_SECRET_KEY"
  [ -n "$ANTHROPIC_API_KEY" ]    && echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"
  [ -n "$TWILIO_SID" ]           && echo "TWILIO_SID=$TWILIO_SID"
  [ -n "$TWILIO_TOKEN" ]         && echo "TWILIO_TOKEN=$TWILIO_TOKEN"
  [ -n "$MY_PHONE_NUMBER" ]      && echo "MY_PHONE_NUMBER=$MY_PHONE_NUMBER"
  [ -n "$GOOGLE_API_KEY" ]       && echo "GOOGLE_API_KEY=$GOOGLE_API_KEY"
  # LLM gateway providers (Sesion 1+2 del plan)
  [ -n "$GROQ_API_KEY" ]         && echo "GROQ_API_KEY=$GROQ_API_KEY"
  [ -n "$DEEPSEEK_API_KEY" ]     && echo "DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY"
  [ -n "$OPENROUTER_API_KEY" ]   && echo "OPENROUTER_API_KEY=$OPENROUTER_API_KEY"
} > .env

echo "=== TASK=$TASK | $(date) ==="

_push_results() {
  # Clona el repo en /tmp, copia los resultados y pushea.
  # Usa directorio único por TASK+PID para evitar race condition entre daily y monitor.
  [ -z "$GH_TOKEN" ] && return 0
  local REPO_URL="https://alpha-bot:${GH_TOKEN}@github.com/sfelix23/alpha-agent.git"
  local PUSH_DIR="/tmp/_push_${TASK:-run}_$$"
  rm -rf "$PUSH_DIR"
  git clone --depth=1 "$REPO_URL" "$PUSH_DIR" 2>/dev/null || { echo "git clone failed, skip push"; return 0; }
  cp /app/signals/latest.json           "$PUSH_DIR/signals/" 2>/dev/null || true
  cp /app/signals/trades.db             "$PUSH_DIR/signals/" 2>/dev/null || true
  cp /app/signals/allocation.json       "$PUSH_DIR/signals/" 2>/dev/null || true
  cp /app/signals/equity_snapshots.json "$PUSH_DIR/signals/" 2>/dev/null || true
  cp /app/signals/workflow_status.json  "$PUSH_DIR/signals/" 2>/dev/null || true
  cp /app/docs/index.html               "$PUSH_DIR/docs/"    2>/dev/null || true
  cd "$PUSH_DIR"
  git config user.name  "alpha-bot"
  git config user.email "alpha-bot@users.noreply.github.com"
  git add signals/ docs/ 2>/dev/null || true
  git diff --staged --quiet || git commit -m "chore: ${TASK:-run} $(date -u +%Y-%m-%dT%H:%M) [skip ci]"
  git push 2>/dev/null || true
  cd /app
  rm -rf "$PUSH_DIR"
}

_validate_signals() {
  # Valida que signals/latest.json exista, sea JSON parseable, tenga el campo
  # esperado y no esté stale (>30 min). Sin esto, si el analyst crashea a mitad
  # el trader puede leer un JSON corrupto y operar con basura.
  python -c '
import json, sys
from pathlib import Path
from datetime import datetime, timezone
p = Path("signals/latest.json")
if not p.exists():
    sys.exit("signals/latest.json no existe")
try:
    data = json.loads(p.read_text(encoding="utf-8"))
except json.JSONDecodeError as e:
    sys.exit(f"signals/latest.json JSON invalido: {e}")
if not any(k in data for k in ("signals", "long_term", "short_term")):
    sys.exit("signals/latest.json sin campos esperados")
age_min = (datetime.now(timezone.utc).timestamp() - p.stat().st_mtime) / 60
if age_min > 30:
    sys.exit(f"signals/latest.json stale ({age_min:.0f}min > 30min)")
print(f"signals/latest.json OK ({age_min:.0f}min de edad)")
'
}

case "$TASK" in
  daily)
    python run_analyst.py --send || { echo "ANALYST FAILED — abortando pipeline"; _push_results; exit 1; }
    _validate_signals             || { echo "SIGNALS INVALIDOS — abortando pipeline";  _push_results; exit 1; }
    python run_trader.py --live       || true
    python run_daytrader.py --live    || true
    python run_dashboard.py --no-open || true
    _push_results
    ;;
  monitor)
    python run_monitor.py --live || true   # no abortar el push si el monitor falla
    python run_dashboard.py --no-open || true
    _push_results
    ;;
  weekly)
    python run_rebalancer.py --live
    ;;
  *)
    echo "ERROR: TASK='$TASK' desconocido. Opciones: daily | monitor | weekly"
    exit 1
    ;;
esac

echo "=== DONE | $(date) ==="
