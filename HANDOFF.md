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

## 🎯 Estado al cierre (iter35, 2026-05-26) — TODO OPERANDO

- ✅ Equity $1,745 (+9%). Cloud Run + schedulers activos. LLM 100% free. CP live **76% win rate** (sube de 73% post-rotación de hoy = match backtest).
- ✅ **iter29 validado**: 5-6 posiciones diversificadas, universo CP diverso (41, tech 29%). Backtest @30bps Sharpe **1.45-1.50** vs SPY 0.94.
- ✅ **iter31 deployado**: gate de rotación de entradas ~mensual. Hoy se ejerció por 1ra vez (`entry_gate.json` escrito 2026-05-26) → cerrado por 20d. El daily ejecutó BUY MU + SELL DDOG; el resto de los días gestiona salidas/backfill, sin churn.
- ✅ **iter32**: vol-penalty REFUTADO (momentum puro gana). Palanca `cp_vol_penalty=0.0` dormida. Fix S&P500 fetch → 503 tickers. Tests core.
- ✅ **iter33 deployado**: backstop pérdida/trade -8% (`max_loss_per_trade_pct`). Corta cola catastrófica, no toca perdedores normales. Helper testeable.
- ✅ **iter34 deployado**: **Cloud Run Service `alpha-bot`** (https://alpha-bot-105138865282.us-central1.run.app) — Telegram + WhatsApp 24/7 SIN PC del usuario. Webhook de Twilio cambiado al cloud (WhatsApp local desconectado por diseño). Comandos cloud-safe: estado/cartera/equity/health/llm/universo/ayuda. PC-específicos (shutdown/wake/run) siguen en dashboard local si se quieren.
- ✅ **iter35 deployado**: **fix del cash drag** (era ~30% deployment vs target 90%). 4 cambios surgicales:
  - `signals.py cap_floor_weights()`: cap por nombre 0.35 (evita que 1 nombre absorba 48% del sleeve) + floor 0.12. Water-filling algorithm, 4 tests.
  - `portfolio.py`: MIN_NOTIONAL 150→75 ($75 es ~4% del equity actual) + dust filter (mv<$5 no cuenta como slot en gate.book_full).
  - `strategy.py`: capital de planificación = equity (no `min(equity,bp)`). bp atado por T+1 settlement subdesplegaba; iter19 headroom sigue regulando downstream.
  - `allocation_agent.py`: cap de la SUMA de sleeves (bug pre-existente: con ec_mult=HOT, cp+opt podían sumar >1).
  - Backtest A/B: Sharpe 1.42 → **1.45**, CAGR +42 → **+43**, Sortino 1.93 → 2.00, DD igual. El cap MEJORA risk-adjusted además de fijar el cash drag. **46 tests**.
- ⚠️ **MIÉRCOLES 05-27 — verificar el 1er daily con iter35**: que despliegue 80-90% (no 30%), 5-6 posiciones reales con notional similar. `python run_dashboard.py --health`.
- ℹ️ **Workspace**: worktree VIEJO (iter10). Código real/deployado en **master `D:/Agente`** (`git -C "D:/Agente"`). NO editar el worktree.
- ℹ️ **Bot URL**: https://alpha-bot-105138865282.us-central1.run.app (Telegram + WhatsApp). Probado y respondiendo.

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

### #1 — Cadencia de rotación de entradas ~MENSUAL ✅ IMPLEMENTADO (iter31) — verificar en vivo
**iter31 deployado:** gate de entrada en `portfolio.diff_against_current(entry_open=...)`. Las salidas/recortes/dust siguen diarias; los **nombres nuevos solo entran 1×/mes (21d)** salvo libro sub-desplegado → backfill (anti cash-drag). Estado en `signals/entry_gate.json` (persiste vía entrypoint). Observable en `python run_dashboard.py --health` (🟢 abierta / 🔵 cerrada + días restantes). **A verificar en vivo:** que el 1er daily post-deploy haga la rotación + registre la fecha, y que los ~21 días siguientes solo gestionen salidas/backfill (no rote nombres nuevos por movimientos marginales de momentum). Si querés afinar la ventana, `_ENTRY_ROTATION_DAYS` en portfolio.py.

<details><summary>Datos que lo respaldan (iter30)</summary>
**Barrido de frecuencia validado (universo amplio, top-5, 2y OOS, costo realista 30bps):**
| Cadencia | Sharpe @10bps | Sharpe @30bps | Turnover | Max DD |
|---|---|---|---|---|
| Semanal (5d) | 1.51 | **1.62** | ~1900-2100% | -24% |
| Mensual (21d) | 1.13 | **1.50** | ~720-790% | **-19%** |
| Trimestral (63d) | 0.75 | — | 292% | — |

**Conclusión:** mensual ≈ semanal en Sharpe (1.50 vs 1.62) pero con **1/2.5 del turnover Y mejor drawdown (-19% vs -24%)**. Trimestral mata el edge (el momentum decae). **El live corría DAILY → sobre-operaba**: pagaba turnover de semanal-plus sin ganar Sharpe, y en cuenta <$25k el spread real erosiona más que los 30bps modelados. Por eso iter31 puso el gate mensual.
</details>

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
