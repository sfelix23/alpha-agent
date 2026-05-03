# CLAUDE.md — Agente Financiero Autónomo

**Usuario:** NAF — economista argentino, Master Finanzas+Big Data, construye agentes de IA en VS Code.
**Proyecto:** Trading autónomo, 69 activos, CAPM/Markowitz/Kelly, Alpaca paper trading, notificaciones WhatsApp.
**Capital:** $1600 USD base · paper trading · compound growth automático.

> Para detalles completos (universo, scheduler, credenciales, arquitectura): @CLAUDE-DETAILS.md

---

## Arquitectura — 4 agentes + orquestador

| Agente | Script | Función |
|--------|--------|---------|
| Analista | `run_analyst.py` | CAPM+Markowitz+news → `signals/latest.json` + WhatsApp |
| Trader | `run_trader.py` | Ejecuta órdenes Alpaca desde signals |
| Monitor | `run_monitor.py` | Cada 30min: stops/TPs/trailing (solo alerta si actúa) |
| Midday | `run_midday.py` | 14:00 ART: CP scan técnico (RSI/MACD/sweep) |
| Scalper | `run_scalper.py` | ORB 15min WebSocket — cuenta separada |
| DayTrader | `run_daytrader.py` | Gap+VWAP+candle intraday — cuenta separada |
| Orquestador | `run_autonomous.ps1` | Task Scheduler 10:35 ART lun-vie |

---

## Archivos clave

```
alpha_agent/config.py          ← ÚNICO lugar para cambiar universo y parámetros
alpha_agent/analytics/kelly.py ← Kelly blend GARCH(1,1) 60% + hist 40%
alpha_agent/analytics/garch.py ← GARCH forecast + CVaR 95%
alpha_agent/daytrading/scanner.py ← _candle_strength() incluido
trader_agent/brokers/alpaca_broker.py ← paper=True, min DTE=30 días
signals/latest.json            ← contrato Agente1→2/3
signals/trade_db.sqlite        ← historial SQL de trades
```

---

## Parámetros clave

| Parámetro | Valor |
|-----------|-------|
| Sleeves | LP 55% · CP 25% · OPT 10% · DT separado |
| Kill switch | -3% equity intradía |
| Trailing stop | +5%→breakeven · +10%→protege 50% profit |
| Max β LP | 1.5 · Min Sharpe LP 0.4 · Top LP 4 · Top CP 2 |
| Options | Long-only · min DTE 30 días · BEAR/VIX>25→hedge puts SPY |
| Cuenta Alpaca | PA3XR9LQ370F · paper · Level 3 options · fractional ON |

---

## Comandos frecuentes

```powershell
python run_analyst.py --send --no-ai        # analyst manual
python run_trader.py --live                 # trader manual
python run_monitor.py --live                # monitor manual
python run_midday.py --live                 # midday scan
python run_scalper.py --dry-run             # scalper test
Get-Content logs\autonomous_$(Get-Date -Format 'yyyy-MM-dd').log -Tail 50
```

---

## Estado actual

**Operativo:** Pipeline completo · Equity $1606 · Trade DB 7 cerrados (win rate 43%)

**Archivos de señales importantes:**
- `signals/trades.db` ← SQLite (no `trade_db.sqlite` — ese nombre está desactualizado en docs)
- `signals/discovery.json` ← candidatos fuera del universo (se escribe cada vez que corre el analyst)
- `signals/equity_snapshots.json` ← historial diario de equity escrito por el monitor
- `signals/allocation.json` ← LP/CP/OPT pcts del último allocation agent

**Implementado recientemente (mayo 2026):**
- LP sleeve re-habilitado: allocation_agent tenía `lp_pct=0.0` hardcodeado → corregido (BULL=45% LP)
- Trade reconciliation: SELLs en trade_db se matchean con BUYs → P&L real calculado
- Discovery.json: el analyst guarda candidatos con prioridad/razon para el dashboard
- Discovery en WhatsApp HALLAZGOS: top 3 tickers con momentum
- Equity snapshots diarios: monitor guarda equity cada 30min → dashboard tiene historial persistente
- VIX spike detection: usa datos intraday 5min (no cierre diario)
- VWAP filter en midday scan: descarta tickers bajo VWAP intradía
- Dashboard: mes actual en calendario P&L (corregido), auto-reconcile trades
- BOM fix: run_autonomous.ps1 ahora escribe last_run.json sin BOM (Python lo parsea correctamente)
- Backtest: Sortino, Calmar, Win Rate, Profit Factor añadidos
- MCPs activos: sequential-thinking · memory · fetch · context7 · tavily · github · stockflow · trade-db · time · filesystem

**Pendiente:**
- WhatsApp bidireccional · VPS migration (Oracle Cloud)
