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
} > .env

echo "=== TASK=$TASK | $(date) ==="

_push_results() {
  # Clona el repo en /tmp, copia los resultados y pushea
  [ -z "$GH_TOKEN" ] && return 0
  local REPO_URL="https://alpha-bot:${GH_TOKEN}@github.com/sfelix23/alpha-agent.git"
  cd /tmp
  rm -rf _push
  git clone --depth=1 "$REPO_URL" _push 2>/dev/null || { echo "git clone failed, skip push"; cd /app; return 0; }
  cp /app/signals/latest.json          _push/signals/ 2>/dev/null || true
  cp /app/signals/trades.db            _push/signals/ 2>/dev/null || true
  cp /app/signals/allocation.json      _push/signals/ 2>/dev/null || true
  cp /app/signals/equity_snapshots.json _push/signals/ 2>/dev/null || true
  cp /app/docs/index.html              _push/docs/    2>/dev/null || true
  cd _push
  git config user.name  "alpha-bot"
  git config user.email "alpha-bot@users.noreply.github.com"
  git add signals/ docs/ 2>/dev/null || true
  git diff --staged --quiet || git commit -m "chore: daily $(date -u +%Y-%m-%dT%H:%M) [skip ci]"
  git push 2>/dev/null || true
  cd /app
}

case "$TASK" in
  daily)
    python run_analyst.py --send
    python run_trader.py --live       || true
    python run_daytrader.py --live    || true
    python run_dashboard.py --no-open || true
    _push_results
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
