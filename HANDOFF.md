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
4. **Perfil AGGRESSIVE pero DIVERSIFICADO** (`config.risk_appetite`): Kelly 0.65, bandas anchas (kill -13%), anti-martingala. iter29: **5-6 posiciones** (no 2 — ver hallazgo abajo). Es paper, objetivo = estrategia sólida para pasar a real.
5. **`dashboard/app.py` NO está en el repo** (local en `D:/Agente/`). DT y scalping **desactivados**.
6. **Tests: 27** (`test_llm_gateway.py`, `test_universe.py`, `test_exits.py`). scoring/monitor sin tests profundos — cuidado.

---

## 🧠 HALLAZGO CENTRAL (iter25-29) — leé esto antes de tocar la estrategia
El backtester debiasado reveló la verdad del edge. **Cada sesgo que se saca, el número baja a la realidad:**
- 118% CAGR / Sharpe 2.07 (25 tech curados + look-ahead) → puro sesgo
- 75% / 1.63 (sin look-ahead)
- 30% / **0.68** (universo tech-heavy, top-2) → ¡PEOR que SPY 0.94!
- **53% / Sharpe 1.13 / DD -33% / win 73% (top-5 diversificado)** ← config actual ✅ supera SPY

**La lección #1: diversificar el SIZING (5-8 posiciones) es la mejora risk-adjusted más grande — NO concentrar en 2.** Concentrar en top-2 daba -41% DD / Sharpe 0.68. Subir a top-5 mejoró TODO a la vez (CAGR, Sharpe, DD, win rate). Diversificar el *universo* solo no alcanza (momentum igual elige los 2 más volátiles); la palanca es el N° de posiciones. **NO vuelvas a concentrar en 2-3 sin re-backtestear.**

---

## 🎯 Estado al cierre (iter29, 2026-05-23) — TODO OPERANDO

- ✅ Equity ~$1.72k (+7-8%). Cloud Run + schedulers activos. LLM 100% free.
- ✅ **iter29 deployado**: 5-6 posiciones diversificadas, universo CP diverso (41, tech 29%).
- ⚠️ **Verificar el daily del LUNES**: que opere con 5-6 posiciones + mantenga despliegue alto (el fix de cash drag iter24 es nuevo).

### Lo hecho esta sesión (iter11→29, todo en master + deployado)
| Iter | Cambio |
|---|---|
| 11-15 | dashboard botones/observable, segundo cerebro, perfil AGRESIVO, DT/scalp OFF |
| 16-17 | scale-in 85% + opt fallback · universo recortado + rotación auto + gate liquidez |
| 18-19 | winner protection + fix reconciliación · **fix cash drag headroom** (44%→76%) |
| 20-22 | SELL clamp + PDT grácil · dust sweep + panel universo · fix falsas alarmas health/watchdog |
| 23-24 | spam MSTR dust + backfill holds · **no rotar no-perdedores a cash** + dashboard DT/scalp banners |
| 25 | **backtester debiasado** (look-ahead off + universo amplio + `--broad` S&P500) |
| 28 | universo CP diverso (tech 72%→29%) + tech boost 0.65→0.15 |
| 29 | **diversificar sizing 2→5/6 posiciones** → Sharpe 0.68→1.13 (supera SPY) |

---

## 🔴 Pendientes (backlog priorizado)

### #1 — Reducir turnover (787%/año es altísimo)
Con slippage real (20bps+) el turnover erosiona el +53%. Probar rebalanceo menos frecuente / más histéresis en la rotación (el winner-protection iter24 ya ayuda). Backtest en curso: `--rebalance 42 --cost-bps 20`. El live corre daily; bajar churn = más hold-protection o rebalance menos seguido.

### #2 — Verificar daily del lunes (cash deploy + 5 posiciones live)
El fix de cash drag (iter24) + 5 posiciones (iter29) son nuevos. Confirmar en el daily del lunes que despliega ~90% en 5-6 nombres diversos (no vuelve a 33% ni a 2 posiciones).

### #3 — Webhook Telegram en Cloud Run (control remoto sin PC prendida)
Hoy el bot = Flask local + ngrok → necesita la PC encendida. Un mini Cloud Run Service que reciba el webhook de Telegram haría que el bot ande 24/7. Reusar `_dispatch_command` de `dashboard/app.py`.

### #4 — Guard PDT proactivo
Cuenta <$25k → Alpaca bloquea >3 day-trades/5d. Ya se maneja grácil (iter20, no spamea), pero un guard que cuente `account.daytrade_count` y evite intentar el day-trade sería más limpio.

### #5 — Confiabilidad sleeve opciones
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
