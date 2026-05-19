# SYSTEM_CONTEXT.md — Compressed system reference

> **Para Claude (y NAF): leé esto PRIMERO antes de hacer cambios.** Reemplaza la necesidad de re-explorar el repo. Última actualización: 2026-05-19 iter4.

## TL;DR (30 segundos)

Sistema de trading autónomo multi-agente. **$1600 USD paper Alpaca**. Corre en **Google Cloud Run Jobs** (`alpha-agent-2025` us-central1) disparado por **Cloud Scheduler**. Bot **WhatsApp + Telegram** (Flask local + ngrok). **LLM gateway multi-provider gratis** (Groq + Gemini + DeepSeek + OpenRouter) con **Anthropic OFF por flag** (anti-flag de cuenta). Stack defensivo: **WAL SQLite, atomic writes, retries con backoff, risk bands escaladas, kill switches dispersos**.

## Estado al 2026-05-19

- **Equity actual**: ~$1664 (+4% vs baseline $1600)
- **Alpha 1m**: +0.49% vs SPY, -5% vs QQQ (sesgo anti-tech)
- **Trades all-time**: 21 LP/CP, 0 DT, 6 SCALP
- **Win rate**: 60-70% (N pequeño, no significativo)
- **Sortino**: 3.24 (con N=10, ruido estadístico)
- **Régimen**: BULL, VIX ~17-18

## Arquitectura (4 capas)

```
┌──── CLOUD RUN JOBS (proyecto alpha-agent-2025) ──────────────────────┐
│ alpha-daily   10:40 ART lun-vie  → run_analyst.py + run_trader.py    │
│                                     + run_daytrader.py + dashboard   │
│ alpha-monitor cada 30min 11-16    → run_monitor.py (live=True)       │
│ alpha-weekly  vie 15:00            → run_rebalancer.py               │
└──────────────────────────────────────────────────────────────────────┘
        ↓ persistencia
┌──── REPO github.com/sfelix23/alpha-agent (master) ───────────────────┐
│ signals/latest.json    contrato analyst → trader/monitor             │
│ signals/trades.db      SQLite (WAL) historial                        │
│ signals/llm_*          budget, cache, provider state                 │
│ signals/equity_snapshots.json  histórico para equity curve trading   │
│ docs/index.html        dashboard estático (regenerado cada job)      │
└──────────────────────────────────────────────────────────────────────┘
        ↓ control bidireccional
┌──── DASHBOARD LOCAL (PC de NAF, no en repo) ─────────────────────────┐
│ D:/Agente/dashboard/app.py    Flask :5050 + ngrok                    │
│ Webhooks: /webhook/whatsapp + /webhook/telegram                      │
│ Comandos: estado, cartera, equity, run, logs, llm, health,           │
│           despertar, sleep, apagar (shutdown 60s), cancelar          │
│ start_dashboard.ps1 → Task Scheduler "Alpha Dashboard" 09:55 ART     │
└──────────────────────────────────────────────────────────────────────┘
        ↓ scalper (no Cloud Run, requiere WebSocket persistente)
┌──── PC LOCAL via Task Scheduler ────────────────────────────────────┐
│ Alpha Scalper      → run_scalper.py (cuenta ALPACA_SCALP_*)         │
└──────────────────────────────────────────────────────────────────────┘
```

## Archivos clave (orden de importancia)

### Configuración y core
- **alpha_agent/config.py** (~400 líneas) — universo (49 tickers), `PARAMS`, `LLM` config, `setup_agent_logging`, `LLM_CASCADE_BY_PURPOSE`. **`LLM.enable_anthropic` es @property que lee env var `ENABLE_ANTHROPIC`** (default OFF, política anti-flag).
- **alpha_agent/news/claude_analyst.py** (~1055 líneas, MONOLITO) — LLM gateway multi-provider + 5 providers (Groq/Gemini/Anthropic/DeepSeek/OpenRouter) + budget tracker + cache SQLite + rate limiter local + funciones de análisis (`assess_position`, `build_macro_narrative`, `wall_street_analysis`, `risk_debate`, `score_event_impact`). `call_llm(prompt, purpose=, max_tokens=, cache_key_extra=)` es la entrada única.
- **alpha_agent/news/sentiment.py** — sentiment batch via `call_llm(purpose="sentiment")`, fallback keywords.
- **alpha_agent/analytics/scoring.py** (~639 líneas) — `build_scores(capm, technical, *, closes, regime, ...)`. **15+ overlays** (RSI, MACD, OBV, BB squeeze, golden cross, gaps, sector boost, quality, F&G, sentiment). Tech BULL boost +0.65 desde iter3. Quality cap variable por régimen (BULL +1.20).
- **alpha_agent/analytics/allocation_agent.py** — `decide_allocation(regime, vix, capital)` → `AllocationDecision(lp_pct, cp_pct, opt_pct, n_cp_positions, cp_max_hold_days, reasoning, level)`. **LP siempre 0%, CP 45-88%, hold 4-10d**. Sleeve modulator desde iter3 (drawdown × equity_curve, NO regime — para no doble-modular).
- **alpha_agent/analytics/kelly.py** — Kelly + composite_kelly_multiplier + adaptive_trailing + equity_curve_multiplier + **risk_action_for_drawdown** (bandas: NORMAL/REDUCE/CLOSE_LOSERS/CLOSE_LONGS/KILL).
- **alpha_agent/analytics/trade_db.py** — SQLite WAL + capital_reservations + multi-account aggregator + rolling_sharpe_by_sleeve. `init_db()` se ejecuta en module-load.
- **alpha_agent/analytics/earnings_guard.py** — `get_earnings_soon(tickers, days=3)`. Llamado en scoring Y en strategy.py pre-BUY desde iter2.

### Brokers y ejecución
- **trader_agent/brokers/alpaca_broker.py** — `AlpacaBroker(paper=True)`. **`_submit_with_retry(req)`** con backoff (0.25/0.55/1.15s) desde iter2 — sólo 5xx/timeouts, 4xx levanta directo.
- **trader_agent/strategy.py** — `execute(broker, dry_run=, max_capital=)`. Pipeline: capital → VIX/regime sizing → build_target_portfolio → diff_against_current → scale-in → stale guard → **validador signals (iter4: stop<TP, R/R>1.5)** → risk_debate → **earnings guard pre-BUY (iter2)** → macro guard → execute orders.
- **trader_agent/portfolio.py** — `build_target_portfolio`, `build_option_intents`, `_from_signal` (con budget tracker para opciones).

### Run scripts
- **run_analyst.py** (~693 líneas) — pipeline analyst. Idempotency guard via `signals/last_run.json`.
- **run_monitor.py** (~1300 líneas) — stops/TPs/trailing/risk_bands. Lockfile `signals/.monitor.lock` (timeout 30s desde iter3). **Watchdog (iter4)**: alerta si signals stale > 24h. Risk bands escaladas (iter3): NORMAL/REDUCE/CLOSE_LOSERS/CLOSE_LONGS/KILL.
- **run_trader.py** — wrapper de `trader_agent.strategy.execute`.
- **run_daytrader.py** — cuenta DT separada. **`_resolve_dt_budget()` dinámico (iter3)** vía `min(equity × 0.875, available_capital("DT"))`. Hoy DT operó 0 trades.
- **run_midday.py, run_scalper.py, run_rebalancer.py** — auxiliares.
- **run_dashboard.py** (~2846 líneas) — genera `docs/index.html`. **`--health` flag (iter3)** imprime snapshot CLI <1s.
- **run_premarket.py, run_eod_report.py, run_portfolio_review.py, run_health_check.py, run_performance.py, run_email_digest.py** — auxiliares con kill switches Anthropic.

### Bot WhatsApp/Telegram (NO en repo)
- **D:/Agente/dashboard/app.py** — Flask + ngrok. `_dispatch_command(body)` único para WA y TG. Webhooks `/webhook/whatsapp` y `/webhook/telegram` (whitelist por chat_id).
- **D:/Agente/start_dashboard.ps1** — levanta Flask + ngrok. Task Scheduler "Alpha Dashboard" 09:55 ART.
- **alpha_agent/notifications/telegram.py** — outbound `send_telegram(text)`.
- **alpha_agent/notifications/whatsapp.py** — outbound `send_whatsapp(text)`.

### Módulos con kill switch Anthropic (iter3)
Estos archivos llamaban a `anthropic.Anthropic()` directo bypassing el gateway. Ahora todos chequean `LLM.enable_anthropic` al inicio y devuelven defaults safe si OFF:

- `alpha_agent/swarm/agents.py::_haiku` → "NO-GO|0|llm_disabled"
- `alpha_agent/swarm/orchestrator.py::_meta_agent` y `_meta_agent_lp` → veredicto deterministic por consenso
- `alpha_agent/scalping/swarm_scalp.py::_haiku` → "NO-GO|llm_disabled"
- `alpha_agent/decision_committee.py::_haiku` y `_meta_agent` → idem
- `alpha_agent/analytics/market_predictor.py::_ai_synthesis` → heuristica por composite_score
- `alpha_agent/news/edgar_monitor.py::_analyze_with_sonnet` → return None
- `alpha_agent/discovery/universe_scanner.py::_claude_synthesize` → ranking por Sharpe si flag OFF
- `run_eod_report.py` y `run_portfolio_review.py::_analyze_with_claude` → resumen heuristico

## Flujos críticos

### Flujo daily (10:40 ART)
```
Cloud Scheduler "sched-alpha-daily"
    → Cloud Run Job alpha-daily (Docker image latest)
        → entrypoint.sh: reconstruye .env con secrets del Secret Manager
            → TASK=daily case:
                1. python run_analyst.py --send
                   - download universe (yfinance, cache pickle)
                   - macro snapshot (VIX, WTI, gold, régimen)
                   - CAPM por activo
                   - discovery agent (universe_scanner, opcional)
                   - sentiment via call_llm purpose=sentiment (Groq → Gemini → keywords)
                   - build_scores con 15+ overlays
                   - allocation_agent.decide_allocation con sleeve modulator
                   - Markowitz + Kelly blend
                   - build_signals → signals/latest.json (atomic write)
                   - WhatsApp/Telegram brief
                2. _validate_signals (entrypoint.sh): JSON exists, parseable, <30min, has expected fields
                3. python run_trader.py --live
                   - VIX adaptive sizing
                   - regime multiplier
                   - F&G multiplier
                   - build target portfolio
                   - diff_against_current vs Alpaca positions
                   - scale-in (60% para nuevos)
                   - stale signal guard
                   - VALIDADOR SIGNALS (iter4): rechaza si stop>TP, R/R<1.5
                   - risk_debate (LLM via gateway, default PROCEED si LLM OFF)
                   - EARNINGS GUARD pre-BUY (iter2)
                   - macro calendar guard
                   - submit orders via _submit_with_retry
                4. python run_daytrader.py --live
                   - DT budget dinámico (iter3)
                   - DT scanner (gap+VWAP+RSI 42-74)
                   - 1 posición concentrada
                5. python run_dashboard.py --no-open
                6. _push_results: git clone /tmp → copy signals/+docs/ → push master
```

### Flujo monitor (cada 30min)
```
Cloud Scheduler sched-alpha-monitor-1,2
    → Cloud Run Job alpha-monitor
        → run_monitor.py --live
            - Filelock signals/.monitor.lock (timeout 30s iter3)
            - WATCHDOG iter4: alerta si signals stale >24h
            - Si mercado cerrado → return (no LLM calls)
            - Cargar signals/latest.json
            - Risk bands escaladas iter3:
                drawdown 0..-2%   → NORMAL (sigue)
                -2..-4%           → REDUCE (escribe signals/reduce_mode.flag)
                -4..-6%           → CLOSE_LOSERS (cierra posiciones con P&L<0)
                -6..-8%           → CLOSE_LONGS (cierra equity longs, mantiene hedge)
                <-8%              → KILL (close_all_positions)
            - Por cada posición:
                - heurística determinista (near_stop o action==CLOSE)
                - SI flagea → assess_position via call_llm (purpose=assess_position)
                - adaptive_trailing(conviction, regime)
                - chandelier_stop por régimen (ATR mult BEAR 2.0 / LATERAL 2.8 / BULL 3.5)
                - VIX spike detection (intraday 5min)
            - Atomic write de signals/latest.json
            - Options monitoring (SL/TP por DTE)
            - Equity snapshot
            - Dashboard regenera
            - Push results al repo
```

### LLM gateway cascade
```
call_llm(prompt, purpose, max_tokens, cache_key_extra) →
    1. Cache lookup en signals/llm_cache.sqlite (TTL por purpose)
    2. Cascada de LLM_CASCADE_BY_PURPOSE[purpose]:
       - sentiment/event_score:    groq fast → gemini → keywords
       - assess_position:          groq fast → gemini → anthropic (si flag ON)
       - narrative:                groq fast → gemini → deterministic
       - wall_street:              groq reasoning → deepseek reasoning → openrouter → anthropic deep (si flag)
       - risk_debate:              groq reasoning → gemini → anthropic fast (si flag)
    3. Por cada provider en orden:
       - is_available()? (env var + flag + disabled state)
       - rate_acquire()? (token bucket local)
       - budget exhausted? (kill switch diario)
       - call() → si ProviderDisabled o ProviderError tras retries → siguiente
       - éxito → record_call (budget + cache) → return
    4. Si todos fallan → return None (caller usa heurística)
```

## Decisiones arquitectónicas (no obvias)

1. **LLM monolítico en claude_analyst.py**: 1055 líneas todo junto. NAF prefiere monolitos a módulos nuevos. **NO crear `alpha_agent/llm/`** — ya pasó en iter1 y hubo que deshacerlo.

2. **`LLMConfig` no es frozen**: el bot Telegram puede cambiar `enable_anthropic` en runtime (vía env var con `@property`).

3. **Sleeve modulator NO usa regime_mult**: el `cp_pct` que viene de `_rule_default` YA está modulado por régimen (BULL=83%, BEAR=45%). Aplicar otra vez el regime_mult del composite Kelly (0.5x) sería doble-modulación.

4. **Dashboard vive fuera del repo**: `D:/Agente/dashboard/app.py` NO está en master. Si querés commitearlo en el futuro, **revisar paths absolutos primero**.

5. **Anthropic flag**: NUNCA hacer retry en 4xx. El gateway desactiva el provider 24h. Cuenta puede flagearse otra vez si esto se rompe.

6. **GHA workflows**: `.github/workflows/alpha_*.yml` siguen en el repo pero con cron deshabilitado (sólo `workflow_dispatch`). Cloud Run es el único ejecutor real.

7. **3 cuentas Alpaca** (LP/CP, SCALP, DT): pero DT operó 0 trades all-time. Considerar fusionar en iter5+.

8. **Universo 49 tickers + discovery**: posible sobre-diversificación con $1600. Sesgo a energy/mining/Argentina hace al sistema underperform QQQ en BULL+tech.

## Runbook operacional

### Local commands
```powershell
python run_analyst.py --send --no-ai        # analyst manual
python run_monitor.py --live                # monitor manual
python run_dashboard.py --health            # snapshot rápido
python -m pytest tests/ -q                  # 15 tests del gateway
```

### Status del LLM gateway
```powershell
python -c "from alpha_agent.news.claude_analyst import get_gateway_status; import json; print(json.dumps(get_gateway_status(), indent=2))"
```

### Cloud Run (gcloud)
```bash
# Trigger manual
gcloud run jobs execute alpha-daily --region us-central1 --project alpha-agent-2025 --wait

# Logs últimas 2h
gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=alpha-daily' \
  --limit=80 --project alpha-agent-2025 --format="value(textPayload)" --freshness=2h

# Rebuild + push imagen (tras cambios)
gcloud builds submit --tag us-central1-docker.pkg.dev/alpha-agent-2025/alpha/agent:latest \
  --project alpha-agent-2025 --timeout=20m
for job in alpha-daily alpha-monitor alpha-weekly; do
  gcloud run jobs update "$job" \
    --image us-central1-docker.pkg.dev/alpha-agent-2025/alpha/agent:latest \
    --region us-central1 --project alpha-agent-2025
done

# Activar Anthropic (cuando confíes)
gcloud run jobs update alpha-daily --update-env-vars ENABLE_ANTHROPIC=true \
  --region us-central1 --project alpha-agent-2025

# Apagarlo de vuelta
gcloud run jobs update alpha-daily --remove-env-vars ENABLE_ANTHROPIC \
  --region us-central1 --project alpha-agent-2025
```

### Secret Manager
```bash
gcloud secrets list --project alpha-agent-2025
# Existentes: ALPACA_API_KEY/SECRET_KEY, ALPACA_DT_API_KEY/SECRET_KEY,
#             ANTHROPIC_API_KEY, GOOGLE_API_KEY, GROQ_API_KEY,
#             DEEPSEEK_API_KEY, OPENROUTER_API_KEY (iter3),
#             TWILIO_SID/TOKEN, MY_PHONE_NUMBER, GH_TOKEN
# Sin: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (sólo en .env local porque
#      el bot vive en la PC, no en Cloud Run)
```

### Bot Telegram/WhatsApp
Comandos: `estado`, `cartera`, `equity`, `run`, `logs`, `llm`, `health`, `despertar`, `sleep`, **`apagar`** (shutdown 60s), `cancelar`, `ayuda`.

Setup webhook Telegram one-time:
```powershell
$token  = ((Get-Content D:\Agente\.env | Select-String "TELEGRAM_BOT_TOKEN=") -split "=", 2)[1]
$domain = ((Get-Content D:\Agente\.env | Select-String "NGROK_DOMAIN=") -split "=", 2)[1]
curl "https://api.telegram.org/bot$token/setWebhook?url=https://$domain/webhook/telegram"
```

## Gotchas (cosas que rompen y son raras de debuggear)

1. **signals/latest.json puede quedar stale 16+ días sin alerta** — desde iter4 hay watchdog en el monitor que detecta esto.
2. **MU vino con stop > TP en una corrida** — desde iter4 hay validador en strategy.py.
3. **dashboard/app.py NO está en master** — vive sólo en `D:/Agente/dashboard/`. NO copiar al worktree sin revisión.
4. **PowerShell `<archivo>` se interpreta como redirección** — escapar con comillas.
5. **`monkeypatch.setattr(LLM, "enable_anthropic", True)` falla** porque ahora es @property. Usar `monkeypatch.setenv("ENABLE_ANTHROPIC", "true")`.
6. **Gemini free tier RESOURCE_EXHAUSTED**: se resetea a las 00:00 PT. Si pega 429 → Groq toma el relevo automático.
7. **Twilio sandbox expira cada 72h** — NAF renueva manualmente.
8. **ngrok URL**: si NAF cambió el dominio, el webhook Telegram apunta al viejo. Re-setear webhook.
9. **Cloud Build sube TODO el repo** salvo `.gcloudignore` (creado en iter2). Sin él, sube logs/ y .git/.
10. **WAL en SQLite**: si se modifica `signals/trades.db` localmente, no commitear — el bot remoto puede tener cambios concurrentes.

## Changelog ejecutivo

- **iter0 (2026-05-18)**: apagado cron GHA (era duplicado con Cloud Run, causaba bursts a Anthropic).
- **iter1+2 (2026-05-18)**: LLM gateway multi-provider + WAL + atomic writes + logging + lockfile + risk budget escalado (definido).
- **iter2 (2026-05-19)**: enchufar código muerto de iter1 + robustness gaps (retry submit_order, atomic latest.json, earnings pre-BUY) + bot Telegram bidireccional + `--health` flag + CLAUDE.md/MEMORY.md.
- **iter3 (2026-05-19)**: modo agresivo CP (ret_1m 0.48, tech boost +0.65, hold 8d) + risk bands LIVE en monitor + ENABLE_ANTHROPIC env var + DT_BUDGET dinámico + **kill switches en 6 archivos paralelos** (anti-flag de cuenta).
- **iter4 (2026-05-19)**: validador signals pre-execution (fix bug MU stop>TP) + watchdog del analyst (stale >24h alerta) + este SYSTEM_CONTEXT.md.

## Lectura crítica honesta (mi opinión)

Para no repetir si Claude vuelve sin contexto:

**Lo que está bien**: infraestructura (8/10) — Cloud Run, gateway multi-provider, risk bands escaladas, atomic writes, retries.

**Lo que está mal**: alpha generation (4-5/10) — 15+ overlays en scoring (over-fit), 49 tickers en $1600 (fricción), LP 0% hardcoded sin evidencia, tests cubren ~3% del código, sesgo anti-tech estructural en BULL+tech rally.

**Recomendación para iter5+**: simplificar. Sacar 50% del código complejo, reducir a 1 sleeve concentrado (5 picks), walk-forward serio con factor decomposition. Probable que la mitad del scoring no aporta alpha real.
