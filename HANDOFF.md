# HANDOFF.md — Próxima sesión Claude (o vos desde otra compu)

> **Leeme PRIMERO. Después `CLAUDE.md` + `SYSTEM_CONTEXT.md`. Después arrancá.**
>
> Última sesión grande: **2026-05-21** (Santino + Claude). Sistema deployado **iter22**.
> Este archivo viaja con el `git clone` → es cómo se continúa el proyecto en cualquier máquina.

---

## 🖥️ CÓMO CONTINUAR DESDE OTRA COMPU (ej: la de la facu)

Las sesiones de Claude Code se guardan **locales por máquina** — esta conversación NO se reanuda literal en otra PC. Pero la continuidad está acá: cloná el repo y una sesión NUEVA de Claude lee `CLAUDE.md` + este `HANDOFF.md` y arranca con todo el contexto.

```bash
git clone https://github.com/sfelix23/alpha-agent.git
cd alpha-agent
pip install -r requirements.txt
gcloud auth login
gcloud config set project alpha-agent-2025
```

**Copiar a mano (NO están en git, a propósito):**
- **`.env`** — todas las API keys (Alpaca, Twilio, Groq, Gemini, etc.). Por USB / gestor de contraseñas. NUNCA commitear.
- **`dashboard/`** — el bot Flask local (gitignored). Solo si querés correr el bot desde esa compu.

**Qué funciona desde cualquier compu sin la PC de casa prendida:** todo lo de Cloud Run (deploy, trigger jobs, leer logs, ver el trading) — el trading vive en la nube. Lo único atado a una PC es el **bot de mensajes** (Flask + ngrok).

**No corras el bot/dashboard en 2 compus a la vez** (chocan los webhooks de ngrok). Idealmente: bot en una sola, o pasarlo a Cloud Run (ver pendiente #2).

---

## 🚨 Lo MÁS CRÍTICO (no skipear)

1. **Usuario = Santino Felix** (antes alias "NAF" — misma persona; la cuenta NAF fue la flageada por Anthropic). Prefiere **modificar archivos existentes a crear nuevos**.
2. **Anthropic OFF salvo `ENABLE_ANTHROPIC=true`**. Kill switches en 6 archivos. NUNCA llamar Anthropic por default. Cascada free: Groq → Gemini → DeepSeek → OpenRouter.
3. **Corre en Google Cloud Run** (`alpha-agent-2025`, us-central1). Daily 10:40 ART, monitor c/30min, weekly viernes. NO es local.
4. **Perfil de riesgo AGGRESSIVE** (`config.risk_appetite`): Kelly 0.65, bandas drawdown anchas (kill -13%), anti-martingala, concentración alta. Es paper, objetivo = estrategia sólida para pasar a real.
5. **`dashboard/app.py` NO está en el repo** (local en `D:/Agente/`). DT y scalping **desactivados** (`enable_daytrading/enable_scalping=False`).
6. **Tests: 27** (`tests/test_llm_gateway.py`, `test_universe.py`, `test_exits.py`). scoring/monitor sin tests profundos — cuidado.

---

## 🎯 Estado al cierre (iter22, 2026-05-21) — TODO OPERANDO

- ✅ Equity **$1.711 (+7.0%)** desde baseline $1600.
- ✅ Capital desplegado **~76%** (tras fix del cash drag iter19).
- ✅ Cloud Run: daily + monitor verdes hoy. Schedulers activos.
- ✅ Win rate CP 67%. LLM 100% free ($0, Anthropic intacto).
- ✅ Sistema autónomo: stops, trailing, rotación de universo, winner protection, dust sweep, PDT-safe.

### Lo hecho esta sesión (iter11→22, todo en master + deployado)
| Iter | Cambio |
|---|---|
| 11-12 | Botones dashboard (Flask /api/cmd) + benchmark race vs SPY/QQQ/Buffett |
| 13 | Segundo cerebro (memoria por ticker → scorer) |
| 14 | Perfil AGRESIVO edge-driven (Kelly 0.65, bandas anchas, anti-martingala) |
| 15 | DT/scalp OFF + dashboard observable (despliegue, riesgo/pos, calidad salidas) |
| 16 | scale-in 85% + fallback opciones→equity |
| 17 | universo recortado + **rotación automática** + gate liquidez |
| 18 | winner protection + fix reconciliación hold_days |
| 19 | **fix cash drag** (headroom doble-resta → 44%→76% desplegado) |
| 20 | SELL qty clamp + PDT grácil |
| 21 | dust sweep + panel universo + **backtester apuntado al sleeve CP** |
| 22 | fix falsas alarmas health_check + watchdog (umbral 26h) |

---

## 🔴 Pendientes (backlog priorizado)

### #1 — Backtester debiasing (el grande, para validar antes de real money)
`run_backtest.py` YA corre (iter21 lo apuntó al sleeve CP). PERO el resultado (+118% CAGR) **NO es creíble** — sesgos: (1) universo curado de ganadores recientes = selection bias, (2) look-ahead del segundo cerebro/stopouts en `build_scores`, (3) muestra mínima (2 pos, 12 rebal), (4) costos optimistas. **Pasos**: universo point-in-time/amplio, apagar look-ahead en modo backtest, 3-5y + top-3, slippage real. Sin esto no hay validación estadística para dinero real.

### #2 — Webhook Telegram en Cloud Run (control remoto sin PC prendida)
Hoy el bot = Flask local + ngrok → necesita la PC encendida. Un mini Cloud Run Service que reciba el webhook de Telegram haría que el bot ande 24/7. Reusar `_dispatch_command` de `dashboard/app.py`.

### #3 — Guard PDT proactivo
Cuenta <$25k → Alpaca bloquea >3 day-trades/5d. Ya se maneja grácil (iter20, no spamea), pero un guard que cuente `account.daytrade_count` y evite intentar el day-trade sería más limpio.

### #4 — Confiabilidad sleeve opciones
GOOGL falla seguido en encontrar contrato (se redirige a equity, pero se pierde convexidad). Mejorar selección strike/expiry.

### Menores: tail hedge sistemático · modelado de costos en live · más tests del core.

---

## ⚠️ Gotchas

1. **Flask local deja zombies** en puerto 5050. Si `/api/cmd` da 404: matar PIDs LISTENING (`netstat -ano | findstr :5050` → `Stop-Process -Force`) y relanzar.
2. **El scalper corría elevado** (Task Scheduler). Ya está Disabled. Si reaparece spam "SCALP VETO": matarlo con admin.
3. **PowerShell + emojis** crashea el print (cp1252). Usar `$env:PYTHONIOENCODING="utf-8"` para scripts con emojis (ej: run_backtest).
4. **gcloud update jobs** sale exit 255 por el Select-String, pero el update funciona (las 3 líneas "Updating" aparecen).
5. **El bot necesita la PC prendida** (ver pendiente #2). El trading NO.
6. **Cloud Run stateless**: `_pull_state` clona el repo al inicio de cada job. `cp_universe_overrides.json` se persiste vía pull/push (iter17).
7. **`max_weight_per_asset` NO concentra el CP** (se normaliza dentro del sleeve). La concentración CP la dan n_cp + conviction + floor (signals.py).

---

## 🛠️ Comandos frecuentes

```bash
# Estado
python run_dashboard.py --health
# Trigger / logs
gcloud run jobs execute alpha-daily --region us-central1 --project alpha-agent-2025 --wait
gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=alpha-daily' --limit=80 --project alpha-agent-2025 --format="value(textPayload)" --freshness=2h
# Rebuild + deploy (tras cambios de código)
gcloud builds submit --tag us-central1-docker.pkg.dev/alpha-agent-2025/alpha/agent:latest --project alpha-agent-2025 --timeout=20m
for job in alpha-daily alpha-monitor alpha-weekly; do gcloud run jobs update "$job" --image us-central1-docker.pkg.dev/alpha-agent-2025/alpha/agent:latest --region us-central1 --project alpha-agent-2025; done
# Tests + backtest
python -m pytest tests/ -q
$env:PYTHONIOENCODING="utf-8"; python run_backtest.py --capital 1600
# Bot: estado · cartera · equity · llm · health · memoria · universo · veto TICKER · pause/resume · apagar
```

**Primer paso recomendado próxima sesión:** `python run_dashboard.py --health` → revisar alertas → seguir con el backtester debiasing (#1).
