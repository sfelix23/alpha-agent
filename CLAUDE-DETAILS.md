# CLAUDE-DETAILS.md — Referencia completa del proyecto

> Cargar con @CLAUDE-DETAILS.md solo cuando sea necesario.

---

## Universo de activos (69 tickers)

**Energía/Petróleo:** XOM, CVX, PBR, SLB, SHEL, TTE, VIST, YPF, PAM, OXY
**Defensa:** LMT, RTX, NOC, GD, BA, PLTR, AVAV
**Argentina:** GGAL, BMA, TGS, EDN, MELI, DESP, IRS
**Minería/Litio/Oro/Uranio:** LAC, SQM, RIO, VALE, GOLD, NEM, FCX, CCJ
**Tech/IA:** NVDA, AMD, MSFT, GOOGL, AAPL, META, TSLA, TSM, ASML, AMZN, MU, ARM, DDOG, NET, MSTR
**Healthcare/Financials:** LLY, ABBV, MRK, JPM, GS, V
**Industrial/Consumo:** CAT, GE, COST, UBER
**Benchmarks/Refugio:** IBIT, ETHE, QQQ, SPY, GLD, TLT

---

## Estructura completa de archivos

```
D:\Agente\
├── run_analyst.py, run_trader.py, run_monitor.py
├── run_midday.py, run_daytrader.py, run_scalper.py
├── run_autonomous.ps1, market_wake.ps1, install_scheduler.ps1
├── alpha_agent/
│   ├── config.py               ← ÚNICO lugar para cambiar universo y parámetros
│   ├── analytics/
│   │   ├── capm.py, markowitz.py, scoring.py, technical.py
│   │   ├── kelly.py            ← half-Kelly blend 60/40 con Markowitz
│   │   ├── garch.py            ← GARCH(1,1) forecast + CVaR 95%
│   │   ├── trade_db.py         ← SQLite historial de trades
│   │   ├── earnings_calendar.py, allocation_agent.py
│   │   └── capital_tracker.py  ← equity virtual base $1600
│   ├── backtest/walkforward.py
│   ├── data/market_data.py     ← yfinance con cache pickle
│   ├── daytrading/scanner.py   ← DT scanner + _candle_strength()
│   ├── scalping/orb_strategy.py, swarm_scalp.py
│   ├── derivatives/options_builder.py, bearish.py
│   ├── macro/macro_context.py  ← VIX, WTI, DXY, oro, yields, régimen
│   ├── news/news_fetcher.py, sentiment.py
│   ├── notifications/whatsapp.py
│   ├── radar/universe_radar.py
│   ├── reasoning/trade_reasoning.py
│   └── reporting/signals.py, ai_report.py
├── trader_agent/
│   ├── brokers/alpaca_broker.py ← paper=True default, min DTE=30 días
│   ├── portfolio.py, strategy.py
├── signals/
│   ├── latest.json             ← contrato Agente1 → Agente2/3
│   ├── trade_db.sqlite         ← historial de trades
│   └── allocation.json, last_run.json
└── logs/autonomous_YYYY-MM-DD.log, analyst_YYYY-MM-DD.log
```

---

## Scheduler — Task Scheduler Windows

| Tarea | Horario ART | Script |
|-------|-------------|--------|
| Alpha Wake | 10:00 lun-vie | market_wake.ps1 |
| Alpha Analyst | 10:35 lun-vie | run_autonomous.ps1 |
| Alpha Monitor | 11:05-16:35 cada 30min | run_monitor.py --live |
| Alpha Midday | 14:00 lun-jue | run_midday.py --live |

Para reinstalar: `.\install_scheduler.ps1` como Administrador.
Para forzar ahora: `Start-ScheduledTask -TaskName 'Alpha Analyst'`

---

## Credenciales (.env)

```
ALPACA_API_KEY / ALPACA_SECRET_KEY   → cuenta paper PA3XR9LQ370F
ALPACA_SCALP_API_KEY / _SECRET_KEY  → cuenta separada para scalper
TWILIO_ACCOUNT_SID / AUTH_TOKEN     → WhatsApp sandbox (renovar cada 72h)
TWILIO_FROM → +14155238886
GOOGLE_API_KEY                      → Gemini (opcional, fallback)
```

---

## Formato del WhatsApp brief (~2400 chars)

```
🤖 ALPHA · fecha · capital · régimen · VIX
📈 MERCADO: tendencias macro 1m
📡 RADAR: top movers con noticias (🔥↑/💥↓)
🧠 HALLAZGOS: mejor pick, alfa promedio LP
📰 EVENTOS CLAVE: top 3 headlines
🎯 DECISIONES: LP/CP/OPT/HEDGE con razón cuantitativa
💰 PROYECCIÓN 12m: retorno esperado / R/R
🛑 PROTECCIÓN: kill switch / stops ATR
```

---

## Notas de arquitectura

- `signals/latest.json` es el contrato entre Agente 1 y Agente 2/3.
- Cache de yfinance en `alpha_agent/data/cache/`. Si hay errores de pickle, se borra solo.
- Las opciones son siempre long-only. Nunca opciones vendidas.
- El hedge (puts SPY) solo se activa en régimen BEAR o VIX > 25.
- Monitor solo manda WhatsApp cuando actúa — silencioso si todo está bien.
- AlpacaBroker filtra contratos con DTE < 30 días antes de matchear opciones.
- Sleeves: LP=55%, CP=25%, OPT=10%, DT=separado (cuenta propia).
- Kelly blend: 60% GARCH(1,1) + 40% histórico para σ en position sizing.
- run_midday.py: detecta liquidity sweeps (HTF low sweep + recovery) como señal CP.
- scanner.py DT: incluye _candle_strength() (body>60%, cierre tercio superior).
