# CLAUDE.md — Agente Financiero Autónomo

**Usuario:** NAF — economista argentino, Master Finanzas+Big Data, construye agentes de IA en VS Code.
**Proyecto:** Trading autónomo, 49 activos, CAPM/Markowitz/Kelly, Alpaca paper trading.
**Capital:** $1600 USD base · paper trading · compound growth automático.
**Infraestructura:** Google Cloud Run Jobs (proyecto `alpha-agent-2025`, region us-central1) + dashboard Flask local con ngrok webhook para WhatsApp/Telegram bidireccional.

> Para detalles completos (universo, scheduler, credenciales, arquitectura): @CLAUDE-DETAILS.md
> Preferencia clave del usuario: **modificar archivos existentes antes que crear nuevos**.

---

## Arquitectura — 4 agentes + orquestador

| Agente | Script | Función |
|--------|--------|---------|
| Analista | `run_analyst.py` | CAPM+Markowitz+news → `signals/latest.json` + WhatsApp |
| Trader | `run_trader.py` | Ejecuta órdenes Alpaca desde signals |
| Monitor | `run_monitor.py` | Cada 30min: stops/TPs/trailing (solo alerta si actúa) |
| Midday | `run_midday.py` | 14:00 ART: CP scan técnico (RSI/MACD/sweep) |
| ~~Scalper~~ | `run_scalper.py` | **DESACTIVADO (iter15)** — `enable_scalping=False`. Sin edge medible. |
| ~~DayTrader~~ | `run_daytrader.py` | **DESACTIVADO (iter15)** — `enable_daytrading=False`. Nunca operó. |
| Orquestador | `run_autonomous.ps1` | Task Scheduler 10:35 ART lun-vie |

---

## Archivos clave

```
alpha_agent/config.py                  ← Universo, PARAMS financieros, LLM gateway config, setup_agent_logging
alpha_agent/news/claude_analyst.py     ← LLM gateway multi-provider (Groq/Gemini/Anthropic/DeepSeek/OpenRouter)
                                         + budget tracker + cache SQLite + rate limiter local
                                         + assess_position, build_macro_narrative, wall_street_analysis,
                                           risk_debate, score_event_impact
alpha_agent/news/sentiment.py          ← Sentiment via call_llm purpose='sentiment' (LLM → keywords fallback)
alpha_agent/analytics/kelly.py         ← Kelly blend GARCH+hist, composite_kelly_multiplier,
                                         risk_action_for_drawdown, kelly_multiplier_for_regime,
                                         adaptive_trailing, equity_curve_multiplier
alpha_agent/analytics/trade_db.py      ← SQLite (WAL+busy_timeout 15s), capital reservations,
                                         rolling_sharpe_by_sleeve, get_combined_state (multi-account),
                                         SEGUNDO CEREBRO: get_ticker_memory/memory_score_adjustment/summarize_learnings
alpha_agent/analytics/allocation_agent.py ← decide_allocation con sleeve modulator (drawdown × equity_curve)
alpha_agent/analytics/scoring.py       ← build_scores, quality bonus cap variable por régimen
alpha_agent/analytics/earnings_guard.py ← get_earnings_soon (chequeado pre-BUY en strategy.py)
alpha_agent/daytrading/scanner.py      ← _candle_strength()
trader_agent/brokers/alpaca_broker.py  ← paper=True, _submit_with_retry (3 intentos backoff)
trader_agent/strategy.py               ← earnings guard pre-BUY + risk debate + macro guard
dashboard/app.py                       ← Flask local (puerto 5050) + ngrok + _dispatch_command
                                         + webhooks /webhook/whatsapp y /webhook/telegram
run_dashboard.py                       ← Genera docs/index.html; flag --health para CLI snapshot
signals/latest.json                    ← contrato Agente1→2/3 (atomic write)
signals/trades.db                      ← historial SQL de trades (WAL activo)
signals/llm_budget.json                ← calls + tokens + costo por provider (reset diario UTC)
signals/llm_provider_state.json        ← providers auto-deshabilitados con TTL
signals/llm_cache.sqlite               ← cache de respuestas LLM con TTL por purpose
signals/capital_reservations.json      ← reservas de capital por sleeve
```

---

## Parámetros clave

| Parámetro | Valor |
|-----------|-------|
| **Perfil de riesgo (iter14)** | **AGGRESSIVE** (edge-driven) · `config.risk_appetite` · Kelly fraccional 0.65 · kelly_alpha 0.50 · anti-martingala con piso anti-ruina |
| Sleeves (BULL niv 1) | LP 0% · CP 90% · OPT 8% · cash 2% (allocation_agent dinámico) |
| Sleeves (BEAR/VIX>30) | LP 0% · CP 45-55% · OPT 5% (defensivo) |
| **Concentración (iter14)** | **n_cp=2** si VIX<18 (concentra mejores ideas) / 3 si VIX≥18 · floor 20%/pos · conviction ALTA ×1.8 → top ~64% (n=2) / ~55% (n=3) · cap blend 0.55 |
| **Kelly regime mult (iter14)** | BULL<15 0.90 · BULL 0.75 · LATERAL 0.60 · BEAR 0.45 · pánico VIX>28 0.35 |
| Risk budget escalado (iter14) | 0..-3% NORMAL · -3..-6% REDUCE 0.7x (entradas ON) · -6..-9% CLOSE_LOSERS · -9..-13% CLOSE_LONGS · <-13% KILL |
| Equity curve (anti-martingala) | HOT 1.35x (piramida racha) · COOLING 0.80x · DEFENSIVE 0.35x · cap sleeve 0.95 |
| Trailing stop adaptive | BULL+ALTA: BE +8% lock 60% a +20%. BEAR+MEDIA: BE +2% lock 30% a +5% |
| Chandelier ATR mult | BULL 3.5 · LATERAL 2.8 · BEAR 2.0 (stop base atr_mult 2.5) |
| Quality multiplier cap | BULL +1.20/-0.60 · LATERAL +0.80/-0.60 · BEAR +0.60/-0.80 |
| **Segundo cerebro (iter13)** | memoria por ticker: favorable (≥60% win, +pnl, ≥3 trades) score +0.25 · adverso (≤34% win, -pnl) score -0.50 |
| Max β LP | 2.8 · Min Sharpe LP 0.10 · Top LP 2 · Top CP 2-3 (agresivo edge-driven) |
| Options | Long-only · min DTE 30 días · BEAR/VIX>25→hedge puts SPY · max 3 contracts/trade · prima ≤$250 |
| Cuenta Alpaca LP/CP | paper · Level 3 options · fractional ON |
| Cuenta Alpaca DT | ALPACA_DT_API_KEY · $1500 budget hardcoded |
| Cuenta Alpaca SCALP | ALPACA_SCALP_API_KEY · WebSocket ORB |
| LLM gateway | Anthropic OFF default (flag) · Groq + Gemini activos · cascada por purpose |
| LLM budget diario | Anthropic $0.10 · Total $0.50 (kill switches) |

---

## Comandos frecuentes

### Locales
```powershell
python run_analyst.py --send --no-ai        # analyst manual
python run_trader.py --live                 # trader manual
python run_monitor.py --live                # monitor manual
python run_midday.py --live                 # midday scan
python run_scalper.py --dry-run             # scalper test
python run_dashboard.py --health            # snapshot rápido del sistema (LLM, capital, sharpe, equity)
python run_dashboard.py --no-open           # regenera docs/index.html sin abrir browser
python -m pytest tests/ -q                  # 15 tests del LLM gateway
Get-Content logs\analyst_$(Get-Date -Format 'yyyy-MM-dd').log -Tail 50
```

### Estado del LLM gateway
```powershell
python -c "from alpha_agent.news.claude_analyst import get_gateway_status; import json; print(json.dumps(get_gateway_status(), indent=2))"
```

### Runbook operacional (Cloud Run)
```bash
# Trigger manual de un job
gcloud run jobs execute alpha-daily   --region us-central1 --project alpha-agent-2025 --wait
gcloud run jobs execute alpha-monitor --region us-central1 --project alpha-agent-2025 --wait
gcloud run jobs execute alpha-weekly  --region us-central1 --project alpha-agent-2025 --wait

# Leer logs recientes (2h hacia atrás)
gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=alpha-daily' \
  --limit=80 --project alpha-agent-2025 --format="value(textPayload)" --freshness=2h

# Listar secrets
gcloud secrets list --project alpha-agent-2025

# Rebuild + push imagen + update jobs (tras cambios de código)
gcloud builds submit --tag us-central1-docker.pkg.dev/alpha-agent-2025/alpha/agent:latest \
  --project alpha-agent-2025 --timeout=20m
for job in alpha-daily alpha-monitor alpha-weekly; do
  gcloud run jobs update "$job" \
    --image us-central1-docker.pkg.dev/alpha-agent-2025/alpha/agent:latest \
    --region us-central1 --project alpha-agent-2025
done
```

### Bot Telegram/WhatsApp
- Mandar `/help` o `ayuda` para ver comandos disponibles.
- Comandos clave: `estado`, `cartera`, `equity`, `run`, `logs`, `llm`, `health`, `despertar`, `sleep`, **`apagar`** (shutdown 60s con `cancelar` para abortar).
- Setup webhook Telegram (one-time): `curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook?url=https://$NGROK_DOMAIN/webhook/telegram"`.

---

## Estado actual

**Operativo:** Pipeline completo · Equity $1606 · Trade DB 7 cerrados (win rate 43%)

**Archivos de señales importantes:**
- `signals/trades.db` ← SQLite (no `trade_db.sqlite` — ese nombre está desactualizado en docs)
- `signals/discovery.json` ← candidatos fuera del universo (se escribe cada vez que corre el analyst)
- `signals/equity_snapshots.json` ← historial diario de equity escrito por el monitor
- `signals/allocation.json` ← LP/CP/OPT pcts del último allocation agent

**Implementado recientemente (mayo 2026):**

Iter 15 (2026-05-20) — **foco en CP + dashboard observable**:
- **DT y scalping DESACTIVADOS** (data-driven): DT nunca operó (0 trades), SCALP 6 abiertas/0 cerradas/$0 realizado. `config.enable_daytrading=False`, `enable_scalping=False` (reversibles); early-exit en `run_daytrader`/`run_scalper`; sacado del `entrypoint.sh` daily. Foco/capital/medición en CP momentum (único motor con edge).
- **Dashboard 4 mejoras**: (1) medidor de **despliegue de capital** (invertido vs target vs cash — caza sub-despliegue); (2) **riesgo por posición** (stop/dist/​$ en riesgo/días hold); (3) **calidad de salidas** (avg win/loss, profit factor, expectancy + alerta si win-rate alto pero W/L<1); (4) **estado de señales** (ejecutada/skip+motivo) + **memoria expandida** por ticker. Rotación sectorial ya existía.
- **Hallazgo**: CP tiene 70% win pero P&L realizado negativo → cortamos winners temprano / dejamos correr losers (el dashboard ahora lo muestra). Pendiente afinar exits.

Iter 14 (2026-05-20) — **perfil de riesgo AGRESIVO edge-driven**:
- Decisión Santino: tomar más riesgo siempre que el retorno esperado lo justifique (paper → real). Fundamento: Kelly fraccional + mean-variance con menor aversión + anti-martingala, con **piso anti-ruina** (kill -13%, no se compone desde cero).
- **Sizing**: `_HALF_KELLY` 0.5→0.65, `_MAX_F_STAR` 2→3, `kelly_alpha` 0.30→0.50, regime mults subidos.
- **Bandas drawdown más anchas** (vol = ruido): kill -8%→-13%, `max_daily_drawdown` 6%→12%, entradas permitidas hasta -6%.
- **Anti-martingala**: equity curve HOT 1.2→1.35 (piramida), allocation permite ec_mult hasta 1.30 (antes cap 1.0), cap duro CP≤0.95.
- **Concentración**: n_cp=2 si VIX<18, floor 30%→20%, conviction ×1.5→×1.8. CP deploy 88%→90%.
- **Selección**: max_beta 2.0→2.8, min_sharpe 0.30→0.10, rsi_overbought 75→80, atr_stop 2.0→2.5.
- **Convexidad**: weight_options 10%→14%, max_contracts 1→3, prima ≤$250.
- `config.risk_appetite="AGGRESSIVE"` (dial reversible para real money). **CLAVE: la fracción Kelly se normaliza dentro del sleeve → la concentración real del CP la dan n_cp + conviction + floor, NO max_weight_per_asset (eso es para LP/blend).**

Iter 11-13 (2026-05-19):
- **Botones dashboard arreglados** (iter11): `dashboard/app.py` expone `/api/cmd/<action>` (CORS, localhost-only) + ruta `/dashboard`. Los botones usan `fetch()` (los links `t.me/bot?text=` están bloqueados por Telegram). **Abrir `http://localhost:5050/dashboard`** para que funcionen. Si dan 404: matar zombies en puerto 5050 (`netstat -ano | findstr :5050` → Stop-Process).
- **Benchmark race** (iter12): dashboard compara Portfolio vs SPY vs QQQ vs **Buffett (BRK-B)**. Estado actual: 2º de 4 — le gana a SPY y BRK-B, atrás de QQQ.
- **"Agresivo con control"** (iter13): `max_weight_per_asset` 0.50→0.40; `allocation_agent` n_cp floor a 3 nombres (antes 1-2) → con floor 30%/pos + conviction ×1.5, el top llega a ~40% sin riesgo catastrófico de un solo nombre. Despliega el sleeve completo (mata el cash drag de 23%).
- **Segundo cerebro** (iter13): `trade_db.get_ticker_memory / memory_score_adjustment / summarize_learnings` — memoria por ticker derivada de trades cerrados (sin tabla nueva). `scoring.py` ajusta score CP (favorable +0.25, adverso -0.50). Panel "🧠 Segundo Cerebro" en dashboard + comando bot `memoria`.

Iter 2 (2026-05-19):
- **Robustness gaps cerrados**: `_submit_with_retry` con backoff 0.25/0.55/1.15s en alpaca_broker (sólo 5xx, NO 4xx); atomic write `signals/latest.json` (tempfile + os.replace) en monitor; filelock timeout 5s → 30s; earnings_guard chequeado **antes** del BUY en strategy.py (no sólo en scoring).
- **Código muerto activado**: `composite_kelly_multiplier` enchufado en `allocation_agent.decide_allocation` (sleeve modulator = drawdown_mult × equity_curve_mult, **excluye** regime_mult para no doble-modular); `adaptive_trailing` reemplaza el `+8% breakeven` fijo en monitor; `_compute_chandelier_stop` ahora acepta `regime` con ATR multiplier variable (BEAR 2.0, LATERAL 2.8, BULL 3.5); `_get_quality_bonus` con cap por régimen (BULL hasta +1.20, BEAR +0.60).
- **Telegram bidireccional**: endpoint `/webhook/telegram` en `dashboard/app.py` (live solo en `D:/Agente/dashboard/app.py`, no en repo). `_dispatch_command` refactor compartido WA + TG. Comandos nuevos: `apagar` (shutdown 60s), `cancelar` (shutdown /a), `llm` (LLM budget), `health` (snapshot).
- **`run_dashboard.py --health`**: CLI snapshot en <1s con Cloud Run last runs, LLM stats por provider, capital reservas, Sharpe rolling 30d por sleeve, equity vs baseline.

Iter 1 (2026-05-18):
- LLM gateway multi-provider en `alpha_agent/news/claude_analyst.py` (Groq + Gemini + Anthropic + DeepSeek + OpenRouter). **Anthropic OFF por flag** para no flagear cuenta. 400/401/403 deshabilita provider 24h sin retry.
- WAL + busy_timeout 15s en `signals/trades.db`.
- `setup_agent_logging()` centralizado en `config.py` con RotatingFileHandler 20MB×7.
- Lockfile en run_monitor (`signals/.monitor.lock`).
- GHA workflows: cron deshabilitado (Cloud Run es el único ejecutor).
- Groq API key en GCP Secret Manager (proyecto `alpha-agent-2025`).

Histórico (antes de mayo 2026):
- LP sleeve re-habilitado, trade reconciliation, discovery.json, equity snapshots diarios, VIX spike intraday, VWAP filter midday, Dashboard calendar P&L, Backtest Sortino/Calmar.

**Pendiente (futuras iter):**
- HTML dashboard cards LLM/Health (sólo CLI por ahora).
- Confirmación robusta para `apagar` (`/apagar YES` 60s) — hoy hay grace 60s + `cancelar`.
- Cloud Run Service para webhook Telegram cuando la PC esté apagada (hoy todo va por dashboard local).
- Drawdown intradía real (no 0.0 placeholder en allocation modulator).
