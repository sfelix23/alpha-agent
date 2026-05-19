# HANDOFF.md — Próxima sesión Claude

> **Leeme PRIMERO. Después leé `SYSTEM_CONTEXT.md`. Después arrancá.**
>
> Última sesión: **2026-05-19** (NAF + Claude Opus 4.7). Sistema deployado iter6.
> Este archivo se actualiza al final de cada sesión grande.

---

## 🚨 Lo MÁS CRÍTICO que tenés que saber (no skipear)

1. **Hay 2 usuarios**:
   - **NAF** — economista argentino, diseñador original del sistema. Construye agentes de IA. Prefiere **modificar archivos existentes a crear nuevos** (decisión clave: en iter1 creé `alpha_agent/llm/` y tuve que deshacerlo).
   - **Santino Felix** — hijo de NAF. También opera el sistema. Identificó iter10 que el sistema lo trataba como NAF, lo cual no era correcto.
   - Si no sabés quién está hablando, **preguntá**.

2. **Anthropic cuenta flageada** (400 "empresa deshabilitada") por uso anómalo. La política es **NUNCA llamarla a menos que `ENABLE_ANTHROPIC=true` esté seteada en env vars de Cloud Run**. Hay **kill switches en 6 archivos paralelos** que verifican el flag. Si cambiás algo del LLM, **respetá esto**.

3. **El sistema corre en Google Cloud Run** (proyecto `alpha-agent-2025`, region us-central1). Daily 10:40 ART, monitor cada 30min, weekly viernes 15:30. **No es local**.

4. **Bot Telegram/WhatsApp existe** pero vive en `D:/Agente/dashboard/app.py` que **NO está commiteado al repo** (es local en la PC de NAF). Si querés modificarlo, tocá ese archivo en `D:/Agente/dashboard/app.py` directo.

5. **Tests cubren sólo el LLM gateway** (15 tests). El resto no tiene tests. Si modificás scoring/allocation/monitor/strategy, andá con MUCHO cuidado — un bug puede hacer perder plata real.

---

## 📍 Cómo arrancar tu sesión (5 min)

```bash
# 1. Leé SYSTEM_CONTEXT.md (5 min) — arquitectura completa
# 2. Estado actual del sistema:
python run_dashboard.py --health

# 3. Verificá si hay alertas pendientes:
gcloud logging read 'resource.type=cloud_run_job' --limit=30 --project alpha-agent-2025 --freshness=24h --format="value(textPayload)" | grep -i "watchdog\|alert\|ERROR\|FAIL"

# 4. Mirá el último brief de WhatsApp/Telegram para entender qué pasó.
```

---

## 🎯 Estado al cierre de iter6 (2026-05-19)

### Sistema operativo
- ✅ Equity: ~$1655 (+3.47% desde baseline $1600)
- ✅ Alpha 1m vs SPY: **+0.59%** (mejorando desde +0.30 a la mañana)
- ⚠️ Alpha 1m vs QQQ: **-4.02%** (portfolio sesgo anti-tech)
- ✅ Sortino 3.24, MaxDD -3.16% (con N=10 — no significativo estadísticamente)
- ✅ Win rate 70.6% all-time
- ✅ LP/CP cuenta: 11 trades, win rate 54.5%, P&L -$57.57
- ✅ DT cuenta: 0 trades
- ✅ SCALP cuenta: 0 trades (en track del repo, pero scalper local podría haber hecho otros)

### Infraestructura
- ✅ 3 Cloud Run Jobs con imagen Docker iter6 deployada
- ✅ Cloud Scheduler activo (alpha-daily 10:40 ART, monitor cada 30min, weekly viernes)
- ✅ Secret Manager: 4 LLM keys (Groq + Gemini + DeepSeek + OpenRouter) + Alpaca + Twilio
- ✅ `ENABLE_ANTHROPIC` env var NO seteada → Anthropic OFF
- ✅ Bot Telegram + WhatsApp bidireccional (Flask + ngrok local en PC)
- ✅ Webhook Telegram seteado

### Bugs CONOCIDOS pero NO arreglados (lo que se rompió hoy y arreglé)
*Estos ya están fixed pero documentados por si reaparecen:*
- **MU stop > TP** (iter4): validador signals lo previene ahora
- **Watchdog falso positivo** (iter5): `_pull_state` clona repo en cada job arranque
- **Dashboard "hace 171h"** (iter6): `_update_workflow_status` updatea el JSON
- **Daily corre varias veces** (iter5): guard del bot `run` requiere `run force` explícito

---

## 🔴 Pendientes URGENTES para próxima sesión

### #1 — Liquidar las 9 posiciones huérfanas (1h, plata real)
Hay 9 posiciones abiertas en Alpaca **sin signal activa en signals/latest.json**:
- 🟢 WOLF P&L +52.8% (winner gordo, no liquidar)
- 🟢 LLY +5.2%, VIST +7.3%, RIO +0.8% (winners chicos)
- 🔴 SQM -9.3%, MSTR -9.5%, TSM -2.9%, GD -1.7%, ARM -0.3% (losers)

**Recomendación**: liquidar los 5 negativos (SQM, MSTR, TSM, GD, ARM) → arrancás limpio. Dejá los 4 positivos con trailing manual.

Cómo: por bot Telegram/WhatsApp no hay comando "sell". Hay que hacerlo desde Alpaca dashboard manual o trigger del trader con un signals/latest.json que tenga sells explícitos.

El **dashboard de iter7** (commit f7424e7 + esta sesión) tiene una card "POSICIONES HUÉRFANAS" que destaca esto visualmente.

### #2 — Walk-forward backtest con factor decomposition (5-8h, alpha real)
`scoring.py` tiene **15+ overlays** (RSI, MACD, OBV, BB squeeze, golden cross, breakout, gaps premarket, intraday momentum, sector boost, F&G, sentiment, quality bonus, tech BULL boost, etc.). Sospecha: **la mitad agrega ruido, no alpha**.

Cómo: backtest 2 años con cada overlay desactivado uno por vez. Sharpe sin el overlay X >= Sharpe con → sacarlo. Resultado esperado: 5-7 factores documentados → más alpha + menos varianza.

`alpha_agent/backtest/walkforward.py` (363 líneas) ya existe. Falta extender para correr en bucle desactivando overlays uno por uno.

### #3 — Tests del core (6-8h, evita regresiones)
Hoy 15 tests en `tests/test_llm_gateway.py`. Falta:
- `tests/test_scoring.py` (con fixtures de capm + technical synthetic)
- `tests/test_allocation_agent.py` (cada régimen)
- `tests/test_kelly.py` (composite_kelly_multiplier, risk_action_for_drawdown)
- `tests/test_strategy.py` (validador signals + earnings guard)

### #4 — Reducir universo a 20 tickers (2h, menos fricción)
49 tickers en $1600 = posiciones de $30-60 → spread/slippage come 30-50bps. Sacar Argentina ADRs (mercado muerto), defense (sector en BEAR), gold miners. Quedarse con tech + financials + energy líquidos.

### #5 — Sleeve rotation activa entre 3 cuentas Alpaca (3-4h)
Función `rolling_sharpe_by_sleeve` existe (`alpha_agent/analytics/trade_db.py`) pero nadie la llama. Sumar al rebalancer semanal: si LP/CP Sharpe -0.4 y SCALP +0.6, reasignar 15% capital. Cap ±50% baseline.

---

## 💡 Mejoras de menor prioridad

- **Reactivar LP sleeve**: `weight_long_term=0.0` hardcoded. Sin evidencia de que CP rotatorio supera. Reactivar con top 3 high-conviction probable +20-30 bps anual.
- **Credit spreads** (vol harvesting cuando VIX>25): el sistema es 100% long-only options. Defined-risk bull put spreads o iron condors agregan 80-120 bps.
- **Form 4 insider intraday**: SEC EDGAR real-time, +60-100 bps según literatura.
- **Stops por ATR del ticker**: hoy stops por % fijo. NVDA con vol 3% diaria no puede tener mismo stop que KO con vol 0.8%.

---

## ⚠️ Gotchas (cosas raras que te van a confundir)

1. **2 dashboards distintos**: `docs/index.html` (HTML estático, regenerado por `run_dashboard.py`) y `dashboard/app.py` (Flask local con webhooks). El bot vive en el segundo, el dashboard visual en el primero.

2. **`dashboard/app.py` NO está en el repo**: vive sólo en `D:/Agente/`. No copiarlo al worktree sin revisar paths absolutos.

3. **PowerShell + `<archivo>` falla**: NAF copia placeholders literales. Usa nombres explícitos.

4. **`monkeypatch.setattr(LLM, "enable_anthropic", True)` falla**: ahora es `@property` que lee env var. Usar `monkeypatch.setenv("ENABLE_ANTHROPIC", "true")`.

5. **Gemini free tier 429**: se resetea a las 00:00 PT. Si pega 429 → Groq toma el relevo automático.

6. **Cloud Run Jobs son stateless**: cada container arranca con la imagen Docker (que tiene signals/* del momento del build). Por eso iter5 sumó `_pull_state` que clona el repo al inicio de cada job.

7. **WAL en SQLite**: `signals/trades.db` puede tener cambios concurrentes entre PC y Cloud Run. No commitearlo localmente sin pull primero.

8. **Twilio sandbox expira cada 72h**: NAF renueva manualmente. Si WhatsApp no llega, esa es la causa.

9. **ngrok URL**: si cambia el dominio, el webhook Telegram apunta al viejo. Re-setear con `curl https://api.telegram.org/bot$TOKEN/setWebhook?url=https://$NGROK_DOMAIN/webhook/telegram`.

10. **`scoring.py` 639 líneas**: NO refactorear sin tests primero. Es el código que más alpha (o ruido) genera.

---

## 🛠️ Comandos frecuentes (copy/paste)

### Trigger manual
```bash
gcloud run jobs execute alpha-daily --region us-central1 --project alpha-agent-2025 --wait
gcloud run jobs execute alpha-monitor --region us-central1 --project alpha-agent-2025 --wait
```

### Logs
```bash
gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=alpha-daily' --limit=80 --project alpha-agent-2025 --format="value(textPayload)" --freshness=2h
```

### Rebuild Docker tras cambios
```bash
gcloud builds submit --tag us-central1-docker.pkg.dev/alpha-agent-2025/alpha/agent:latest --project alpha-agent-2025 --timeout=20m
for job in alpha-daily alpha-monitor alpha-weekly; do
  gcloud run jobs update "$job" --image us-central1-docker.pkg.dev/alpha-agent-2025/alpha/agent:latest --region us-central1 --project alpha-agent-2025
done
```

### Activar Anthropic temporalmente (CON CUIDADO)
```bash
gcloud run jobs update alpha-daily --update-env-vars ENABLE_ANTHROPIC=true --region us-central1 --project alpha-agent-2025
# Apagar:
gcloud run jobs update alpha-daily --remove-env-vars ENABLE_ANTHROPIC --region us-central1 --project alpha-agent-2025
```

### Bot Telegram/WhatsApp commands
```
estado · cartera · equity · pnl · signals · run [force] · llm · health
despertar · sleep · apagar (shutdown 60s) · cancelar · ayuda
```

### Tests + smoke
```bash
python -m pytest tests/ -q
python -c "from alpha_agent.news.claude_analyst import call_llm, get_gateway_status; import json; print(json.dumps(get_gateway_status(), indent=2))"
```

---

## 📜 Iteraciones (changelog ejecutivo)

| # | Fecha | Cambio principal |
|---|---|---|
| iter0 | 2026-05-18 | Apagar GHA cron (causa del flag Anthropic) |
| iter1+2 | 2026-05-18/19 | LLM gateway multi-provider + WAL + atomic + logging + lockfile + risk budget escalado |
| iter3 | 2026-05-19 | Modo agresivo CP + risk bands LIVE + ENABLE_ANTHROPIC env + DT_BUDGET dinámico + **kill switches Anthropic en 6 archivos** |
| iter4 | 2026-05-19 | Validador signals pre-exec (bug MU) + watchdog stale + SYSTEM_CONTEXT.md |
| iter5 | 2026-05-19 | `_pull_state` en entrypoint (fix watchdog falso positivo) + bot `run` guard |
| iter6 | 2026-05-19 | `_update_workflow_status` + push de llm_budget/state + dashboard card LLM Status |
| iter7 | 2026-05-19 | Dashboard cards: Risk Band activa + Régimen+allocation activa + **Posiciones Huérfanas** |
| iter8 | 2026-05-19 | **Auto-handler huérfanas** (regla: P&L>+20% mantener, <-5% close, >7d sin progreso close) + comando `liquidate` + botones interactivos |
| iter9 | 2026-05-19 | Watchdog robusto (no alerta con datos corruptos) + `_pull_state` valida JSON + cooldown 4h |
| iter10 | 2026-05-19 | Comandos `pause`/`resume`/`anthropic on/off`/`force daily` + card **Control Center** con botones + corrección identidad (Santino, hijo de NAF) |

---

## 🎬 Lo último que hizo el sistema (referencia)

- **Daily 12:55 ART**: rotó de Energy a Tech (vendió VIST viejo, compró VIST nuevo $78→$690 target, ARM $215→$510 target, call GOOGL $449→$117/contr)
- **Monitor 16:33 UTC**: ningún alert, todas posiciones in range
- **Sector momentum**: Tech +19.1% (leading), GoldMiners -15.8% (bottom)
- **Régimen**: BULL · VIX 17.0

---

**Tu primer paso recomendado para iter8 o sesión siguiente**:

1. `python run_dashboard.py --health` → snapshot del estado
2. Leé la card "POSICIONES HUÉRFANAS" en `docs/index.html` → decidí con NAF qué hacer
3. Si NAF aprueba: hacer un signals/latest.json con SELL explícito de las 5 negativas + push + trigger alpha-daily.

Suerte. Sistema en buenas manos.
