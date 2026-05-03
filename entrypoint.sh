#!/bin/bash
set -e

# Reconstruct .env from individual env vars injected by Cloud Run Secrets
{
  [ -n "$ALPACA_API_KEY" ]      && echo "ALPACA_API_KEY=$ALPACA_API_KEY"
  [ -n "$ALPACA_SECRET_KEY" ]   && echo "ALPACA_SECRET_KEY=$ALPACA_SECRET_KEY"
  [ -n "$ALPACA_DT_API_KEY" ]   && echo "ALPACA_DT_API_KEY=$ALPACA_DT_API_KEY"
  [ -n "$ALPACA_DT_SECRET_KEY" ] && echo "ALPACA_DT_SECRET_KEY=$ALPACA_DT_SECRET_KEY"
  [ -n "$ANTHROPIC_API_KEY" ]   && echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"
  [ -n "$TWILIO_SID" ]          && echo "TWILIO_SID=$TWILIO_SID"
  [ -n "$TWILIO_TOKEN" ]        && echo "TWILIO_TOKEN=$TWILIO_TOKEN"
  [ -n "$MY_PHONE_NUMBER" ]     && echo "MY_PHONE_NUMBER=$MY_PHONE_NUMBER"
  [ -n "$GOOGLE_API_KEY" ]      && echo "GOOGLE_API_KEY=$GOOGLE_API_KEY"
} > .env

# Configurar git para poder commitear resultados
if [ -n "$GH_TOKEN" ]; then
  git config --global user.name  "alpha-bot"
  git config --global user.email "alpha-bot@users.noreply.github.com"
  git remote set-url origin "https://alpha-bot:${GH_TOKEN}@github.com/sfelix23/alpha-agent.git" 2>/dev/null || true
fi

echo "=== TASK=$TASK | $(date) ==="

case "$TASK" in
  daily)
    python run_analyst.py --send
    python run_trader.py --live       || true
    python run_daytrader.py --live    || true
    python run_dashboard.py --no-open || true
    if [ -n "$GH_TOKEN" ]; then
      git add signals/latest.json signals/trades.db signals/allocation.json \
              signals/equity_snapshots.json docs/index.html 2>/dev/null || true
      git diff --staged --quiet || git commit -m "chore: daily $(date -u +%Y-%m-%dT%H:%M) [skip ci]"
      git push 2>/dev/null || true
    fi
    ;;
  monitor)
    python run_monitor.py --live
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
