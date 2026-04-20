# CLAUDE.md — Agente Financiero Autónomo

> Este archivo es leído automáticamente al inicio de cada conversación.
> Contiene todo el contexto del proyecto para no perder tokens explicando el estado.

---

## Quién soy / contexto

**Usuario:** NAF — economista argentino, Master en Finanzas y Big Data, construye agentes de IA en VS Code.

**Objetivo del proyecto:** Sistema de trading autónomo que analiza 51 activos, toma decisiones basadas en CAPM/Markowitz/beta, y opera automáticamente en Alpaca paper trading. Notifica por WhatsApp.

**Filosofía de inversión:**
- Capital inicial: $1600 USD (paper trading primero, luego real)
- Maximizar ganancia sujeto a protección del capital
- Tomar riesgo si retorno esperado > riesgo asumido
- Las ganancias se reinvierten automáticamente (compound growth)
- Integrar noticias geopolíticas (Trump, Ormuz, etc.) en las decisiones

---

## Arquitectura — 3 agentes

```
Agente 1: Analista (run_analyst.py)
  CAPM + Markowitz + scoring técnico + news/sentiment + macro
  → genera signals/latest.json
  → manda WhatsApp con reporte completo

Agente 2: Trader (run_trader.py)
  Lee signals/latest.json
  → ejecuta órdenes en Alpaca paper
  → kill switch -3% intradía

Agente 3: Monitor intradía (run_monitor.py)
  Corre cada 30 min durante horario de mercado
  → revisa stops/TPs/trailing stops
  → solo manda WhatsApp si actúa (cierra algo)
```

**Orquestador autónomo:** `run_autonomous.ps1` (llamado por Task Scheduler)

---

## Estructura de archivos clave

```
D:\Agente\
├── CLAUDE.md                    ← este archivo
├── .env                         ← credenciales (ver sección)
├── run_analyst.py               ← entry point Agente 1
├── run_trader.py                ← entry point Agente 2
├── run_monitor.py               ← entry point Agente 3 (monitor intradía)
├── run_autonomous.ps1           ← pipeline completo desatendido
├── run_backtest.py              ← backtester walk-forward
├── verify_and_trade.ps1         ← test manual completo
├── market_wake.ps1              ← wake PC + keep alive 10:00-17:15
├── install_scheduler.ps1        ← registra las 3 tareas en Task Scheduler
│
├── alpha_agent/
│   ├── config.py                ← universo (51 activos), parámetros, SECTOR_MAP
│   ├── analytics/
│   │   ├── capm.py              ← CAPM, alpha de Jensen, beta
│   │   ├── markowitz.py         ← optimización frontera eficiente
│   │   ├── scoring.py           ← scoring LP/CP con guards
│   │   └── technical.py         ← RSI, ATR, momentum, stop loss ATR
│   ├── backtest/walkforward.py  ← walk-forward backtester
│   ├── data/market_data.py      ← descarga yfinance con cache pickle
│   ├── derivatives/
│   │   ├── options_builder.py   ← construcción book de opciones direccionales
│   │   └── bearish.py           ← candidatos bajistas para puts
│   ├── macro/macro_context.py   ← VIX, WTI, DXY, oro, yields, régimen
│   ├── news/
│   │   ├── news_fetcher.py      ← yfinance news + Google News RSS (cache diario)
│   │   └── sentiment.py         ← scoring keyword-based
│   ├── notifications/whatsapp.py← Twilio WhatsApp sandbox
│   ├── radar/universe_radar.py  ← escaneo noticioso de los 51 activos
│   ├── reasoning/trade_reasoning.py ← TradeThesis: quant+tech+news+macro+risk
│   └── reporting/
│       ├── signals.py           ← dataclasses Signal, Signals
│       └── ai_report.py         ← signals_to_whatsapp_brief() y brief detallado
│
├── trader_agent/
│   ├── brokers/alpaca_broker.py ← AlpacaBroker (paper=True default)
│   ├── portfolio.py             ← gestión de posiciones
│   └── strategy.py              ← lógica de ejecución
│
├── signals/
│   └── latest.json              ← último output del analyst
└── logs/
    ├── autonomous_YYYY-MM-DD.log
    └── monitor.log
```

---

## Universo de activos (51)

**Energía/Petróleo:** XOM, CVX, PBR, SLB, SHEL, TTE, VIST, YPF, PAM
**Defensa:** LMT, RTX, NOC, GD, BA, PLTR, AVAV
**Argentina:** GGAL, BMA, TGS, EDN, PAMP.BA, MELI, DESP, IRS, ALUA.BA
**Minería/Litio/Oro/Uranio:** ALTM, LAC, SQM, RIO, VALE, GOLD, NEM, FCX, CCJ
**Tech/IA:** NVDA, AMD, MSFT, GOOGL, AAPL, META, TSLA, TSM, ASML, AMZN
**Healthcare/Financials:** LLY, JPM
**Benchmarks/Refugio:** IBIT, ETHE, QQQ, SPY, GLD

---

## Parámetros financieros clave

| Parámetro | Valor |
|-----------|-------|
| Capital paper | $1600 USD |
| Sleeve LP | 55% del capital |
| Sleeve CP | 25% del capital |
| Sleeve OPT | 20% del capital |
| Risk-free rate | 4.5% (US 10y T-Note) |
| History | 2 años (504 días) |
| Max β LP | 1.5 |
| Min Sharpe LP | 0.4 |
| Top N LP | 4 posiciones |
| Top N CP | 2 posiciones |
| Kill switch | -3% equity intradía |
| Trailing stop | +5% → breakeven; +10% → protege 50% profit |
| Alpaca account | PA3XR9LQ370F (paper, Level 3 options, fractional ON) |

---

## Credenciales (.env)

```
ALPACA_API_KEY       → trading en Alpaca paper
ALPACA_SECRET_KEY    → trading en Alpaca paper
TWILIO_ACCOUNT_SID   → WhatsApp sandbox
TWILIO_AUTH_TOKEN    → WhatsApp sandbox
TWILIO_FROM          → número Twilio (+14155238886 sandbox)
TWILIO_TO            → número del usuario (WhatsApp)
GOOGLE_API_KEY       → Gemini (opcional, fallback si no está)
```

**Nota:** El sandbox de Twilio WhatsApp expira cada 72h. Renovar con: mandar "join <código>" al número de Twilio desde el celular.

---

## Scheduler — Task Scheduler de Windows

3 tareas registradas (corren aunque la pantalla esté bloqueada):

| Tarea | Horario | Script |
|-------|---------|--------|
| Alpha Wake | 10:00 ART lun-vie | market_wake.ps1 |
| Alpha Analyst | 10:35 ART lun-vie | run_autonomous.ps1 |
| Alpha Monitor | 11:05-16:35 cada 30min lun-vie | run_monitor.py --live |

**Para reinstalar:** `.\install_scheduler.ps1` como Administrador.
**Para ver estado:** `Get-ScheduledTask | Where TaskName -like 'Alpha*'`
**Para forzar ahora:** `Start-ScheduledTask -TaskName 'Alpha Analyst'`

**Antes de ir a la facultad:** Win+X → Suspender (NO apagar). La PC se despierta sola a las 10:00.

---

## Comandos frecuentes

```powershell
# Test completo manual (desde VS Code terminal en D:\Agente)
.\verify_and_trade.ps1 -SendWhatsApp

# Solo el analyst
python run_analyst.py --send --no-ai

# Solo el trader
python run_trader.py --live

# Monitor de posiciones
python run_monitor.py --live

# Backtest
python run_backtest.py

# Ver logs de hoy
Get-Content logs\autonomous_$(Get-Date -Format 'yyyy-MM-dd').log -Tail 50
```

---

## Estado actual del proyecto

**Operativo y funcionando:**
- Pipeline completo: descarga → CAPM → Markowitz → técnicos → news → señales → órdenes → WhatsApp
- Backtest walk-forward: CAGR 11.18%, Sharpe OOS 0.74, DD -7.57% (2 años de historia)
- WhatsApp brief enriquecido con: radar 51 activos, decisiones con razonamiento (Sharpe/α/RSI), proyección R/R
- Task Scheduler configurado con wake-from-sleep
- Reinversión automática: el capital se actualiza con el equity real de Alpaca antes de cada run

**Problema conocido — wake-from-sleep:**
Si la PC está en Shutdown (apagada) no puede despertar por sí sola.
Debe estar en Sleep/Hibernate. Si alguien la enciende, las tareas se ejecutan
igual aunque no haya sesión iniciada (LogonType Password).

**Implementado recientemente:**
- Kelly Criterion: `alpha_agent/analytics/kelly.py` — half-Kelly blended 60/40 con Markowitz
- Gemini Flash sentiment: `alpha_agent/news/sentiment.py` — Gemini primero, keywords como fallback
- Portfolio rebalancer semanal: `run_rebalancer.py` — viernes 15:00 vía Task Scheduler (umbral 8%)
- Dashboard HTML: `run_dashboard.py` — genera `dashboard.html` con equity, posiciones y señales

**Pendiente / próximas mejoras:**
1. Intraday data de 15 min para CP más precisos
2. WhatsApp bidireccional (necesita Cloudflare Worker como intermediario)
3. Migración a VPS ($5/mes) para eliminar dependencia del laptop

---

## Formato del WhatsApp brief

El mensaje tiene ~2400 chars y esta estructura:
```
🤖 ALPHA · fecha · capital · régimen · VIX
📈 MERCADO: tendencias macro 1m (petróleo/oro/dólar)
📡 RADAR: top movers del universo con noticias (🔥↑/💥↓ + titular + acción del bot)
🧠 HALLAZGOS: mejor pick, alfa promedio LP, sesgo sectorial
📰 EVENTOS CLAVE: top 3 headlines de las tesis
🎯 DECISIONES: por sleeve (LP/CP/OPT/HEDGE) con razón cuantitativa
💰 PROYECCIÓN 12m: retorno esperado / riesgo / ratio R/R
🛑 PROTECCIÓN: kill switch / stops ATR / opciones long-only
```

---

## Notas de arquitectura importantes

- `signals/latest.json` es el contrato entre Agente 1 y Agente 2/3. Contiene toda la tesis por posición.
- Cache de yfinance en `alpha_agent/data/cache/`. Si hay errores de pickle, se borra y redescarga solo.
- `alpha_agent/config.py` es el único lugar donde cambiar el universo, parámetros y sleeves.
- El Agente 3 (monitor) solo manda WhatsApp cuando ACTÚA. Si todo está bien, loguea y sale silenciosamente.
- Las opciones son siempre long-only (riesgo acotado a la prima). Nunca opciones vendidas.
- El hedge de cartera (puts SPY) solo se activa en régimen BEAR o VIX > 25.
