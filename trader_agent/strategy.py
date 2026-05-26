"""
Estrategia de ejecución del trader_agent.

Reglas (v1):
    1. Solo opera con mercado abierto (US equity).
    2. Kill switch: si el drawdown del día supera PARAMS.max_daily_drawdown,
       abortamos y no enviamos NINGUNA orden. El equity al abrir el día se
       persiste en .cache/trader_day_state.json.
    3. Lee signals/latest.json.
    4. Construye target portfolio equity (LP + CP) y diff vs broker.
    5. Construye OptionIntents (directionales + hedge) si el broker soporta.
    6. Envía órdenes (equity primero, opciones después).
    7. Avisa por WhatsApp con resumen de fills.

Flags:
    - dry_run: si True, imprime las órdenes sin enviarlas.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

from alpha_agent.config import PARAMS, PATHS
from alpha_agent.notifications import send_notification as send_whatsapp
from alpha_agent.reporting.signals import Signal, Signals

from .brokers.base import BrokerBase, OptionOrder, Order
from .portfolio import (
    OptionIntent,
    TradeIntent,
    build_option_intents,
    build_target_portfolio,
    check_capital_headroom,
    diff_against_current,
    record_entry_rotation,
    total_invested_notional,
)
from .portfolio import entry_window_open as portfolio_entry_window_open

logger = logging.getLogger(__name__)

_DAY_STATE_PATH = PATHS.cache_dir / "trader_day_state.json"


def load_latest_signals() -> Signals:
    path = PATHS.signals_dir / "latest.json"
    if not path.exists():
        raise FileNotFoundError(f"No hay señales en {path}. Corré primero run_analyst.py")
    data = json.loads(path.read_text(encoding="utf-8"))

    sig = Signals(
        generated_at=data["generated_at"],
        horizon=data["horizon"],
        capital_usd=data.get("capital_usd", PARAMS.paper_capital_usd),
        params=data["params"],
        macro=data.get("macro", {}),
        portfolio=data.get("portfolio", {}),
    )
    sig.long_term = [Signal(**s) for s in data.get("long_term", [])]
    sig.short_term = [Signal(**s) for s in data.get("short_term", [])]
    sig.options_book = [Signal(**s) for s in data.get("options_book", [])]
    sig.hedge_book = [Signal(**s) for s in data.get("hedge_book", [])]
    return sig


# ─────────────────────────────────────────────────────────────────────────────
# Kill switch: anchor al equity del inicio del día y checkeo en cada run
# ─────────────────────────────────────────────────────────────────────────────
def _load_day_state() -> dict:
    if _DAY_STATE_PATH.exists():
        try:
            return json.loads(_DAY_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_day_state(state: dict) -> None:
    _DAY_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _kill_switch_check(broker: BrokerBase) -> tuple[bool, str]:
    """
    Devuelve (ok_to_trade, reason).
    Si el equity de hoy bajó más que PARAMS.max_daily_drawdown respecto al
    anchor del día → False.
    """
    today = date.today().isoformat()
    state = _load_day_state()
    try:
        current_equity = broker.get_equity()
    except Exception as e:
        logger.warning("No pude leer equity: %s — dejo pasar la orden (sin kill switch).", e)
        return True, "equity lookup failed"

    anchor = state.get(today)
    if anchor is None:
        # primer run del día → anclamos
        state = {today: current_equity}
        _save_day_state(state)
        return True, f"anchor intradía seteado en ${current_equity:.2f}"

    drawdown = (anchor - current_equity) / anchor if anchor > 0 else 0.0
    if drawdown > PARAMS.max_daily_drawdown:
        return False, (
            f"KILL SWITCH: drawdown intradía {drawdown*100:.2f}% > "
            f"{PARAMS.max_daily_drawdown*100:.1f}% (anchor ${anchor:.2f} → ahora ${current_equity:.2f})"
        )
    return True, f"drawdown intradía {drawdown*100:+.2f}% dentro del límite"


# ─────────────────────────────────────────────────────────────────────────────
# VIX-adaptive sizing helpers
# ─────────────────────────────────────────────────────────────────────────────
def _get_vix(signals: Signals) -> float:
    try:
        return float(signals.macro.get("prices", {}).get("vix", 18.0))
    except Exception:
        return 18.0


def _vix_multiplier(vix: float) -> float:
    """Reduce capital en alta volatilidad, agresivo en calma (growth mode)."""
    if vix > 30:
        return 0.60
    if vix > 25:
        return 0.75
    if vix < 15:
        return 1.15  # calma extrema → aprovechar al máximo
    if vix < 20:
        return 1.05  # calma moderada → leve bonus
    return 1.0


def _regime_multiplier(regime: str) -> float:
    """Más capital en bull (capturar upside), menos en bear (proteger capital)."""
    r = regime.lower()
    if r == "bull":
        return 1.15  # bull: presionar al máximo
    if r in ("sideways", "lateral", "neutral"):
        return 0.95  # neutral: casi normal, no cortar demasiado
    if r == "bear":
        return 0.65
    return 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Execute
# ─────────────────────────────────────────────────────────────────────────────
def execute(broker: BrokerBase, *, dry_run: bool = True, max_capital: float | None = None) -> list[dict]:
    """
    Ejecuta el plan de trading sobre el broker dado.
    """
    if not broker.is_market_open():
        logger.warning("Mercado cerrado. Abortando ejecución.")
        return [{"status": "market_closed"}]

    ok, reason = _kill_switch_check(broker)
    logger.info("Kill switch: %s (%s)", "OK" if ok else "TRIGGERED", reason)
    if not ok:
        send_whatsapp(
            f"🛑 *KILL SWITCH DISPARADO*\n\n{reason}\n\nNo se enviaron órdenes.",
            header="TRADER ALPHA",
        )
        return [{"status": "kill_switch", "reason": reason}]

    signals = load_latest_signals()

    # Signal freshness guard: señales > 4h no deben usarse para BUYs nuevos.
    # El analyst y trader corren juntos en el daily task (misma hora), así que
    # señales viejas solo aparecen si el analyst falló o el trader corrió suelto.
    _MAX_SIG_AGE_H = 4.0
    try:
        _sig_age_h = (datetime.now() - datetime.fromisoformat(signals.generated_at)).total_seconds() / 3600
    except Exception:
        _sig_age_h = 0.0
    if _sig_age_h > _MAX_SIG_AGE_H:
        logger.warning(
            "⚠️ Señales STALE (%.0fh antiguas, generadas %s) — bloqueando BUYs nuevos",
            _sig_age_h, signals.generated_at,
        )
        _stale_signals = True
    else:
        _stale_signals = False

    # iter35: capital de PLANIFICACIÓN = equity. El bp queda atado por T+1 settlement
    # tras una SELL (cuenta cash) → si usábamos min(equity,bp) se subdesplegaba.
    # El bp restringe DOWNSTREAM via check_capital_headroom (iter19) que hace
    # min(equity-invested, bp) sobre los intents. Así el target se construye con el
    # equity completo (sin cash drag artificial) y el headroom regula lo que se ejecuta.
    # Si bp > equity (margen 2x en Alpaca paper) → seguimos limitando a equity.
    equity = broker.get_equity()
    bp = broker.get_buying_power()
    capital = min(max_capital if max_capital else equity, equity)
    logger.info(
        "Capital: $%.2f (equity=$%.2f, bp=$%.2f, cap_arg=%s)",
        capital, equity, bp, str(max_capital),
    )

    # ── VIX-adaptive sizing ─────────────────────────────────────────
    vix = _get_vix(signals)
    vix_mult = _vix_multiplier(vix)
    capital_adj = capital * vix_mult
    if vix_mult != 1.0:
        logger.info(
            "VIX %.1f → multiplicador %.0f%% → capital ajustado $%.2f (desde $%.2f)",
            vix, vix_mult * 100, capital_adj, capital,
        )
    else:
        logger.info("VIX %.1f → sizing normal (100%%)", vix)

    # ── Regime-adaptive sizing ──────────────────────────────────────────────
    macro = signals.macro or {}
    regime = macro.get("regime", "unknown")
    regime_mult = _regime_multiplier(regime)
    if regime_mult != 1.0:
        capital_adj *= regime_mult
        logger.info(
            "Regime '%s' → multiplicador %.0f%% → capital ajustado $%.2f",
            regime, regime_mult * 100, capital_adj,
        )

    # ── Fear & Greed capital multiplier ─────────────────────────────────────
    fg_mult = 1.0
    try:
        from alpha_agent.data.alternative_data import get_all_alternative_data
        fg_val = get_all_alternative_data([]).get("fear_greed", {}).get("value")
        if fg_val is not None:
            fg_val = int(fg_val)
            if fg_val <= 25:
                fg_mult = 1.15   # extreme fear → buy more
            elif fg_val >= 80:
                fg_mult = 0.85   # extreme greed → reduce size
            if fg_mult != 1.0:
                capital_adj *= fg_mult
                logger.info("Fear&Greed %d → multiplicador %.0f%% → capital $%.2f", fg_val, fg_mult * 100, capital_adj)
    except Exception:
        pass

    fills: list[dict] = [{"status": "meta", "vix": vix, "vix_mult": vix_mult, "regime": regime, "regime_mult": regime_mult, "fg_mult": fg_mult}]

    # ── Equity (LP + CP) ────────────────────────────────────────────
    target = build_target_portfolio(signals, capital_adj)
    positions = broker.get_positions()
    invested = total_invested_notional(positions)
    # iter31: gate de entrada ~mensual. El daily gestiona salidas/stops siempre;
    # los NOMBRES NUEVOS solo entran 1×/mes (salvo libro sub-desplegado → backfill).
    _entry_open = portfolio_entry_window_open()
    if not _entry_open:
        logger.info("Entry gate: ventana mensual CERRADA — daily gestiona salidas, sin nombres nuevos salvo backfill")
    equity_intents = diff_against_current(target, positions, entry_open=_entry_open)
    # iter19: headroom = min(buying_power, equity - invested). Pasamos EQUITY (no el
    # capital ya capeado por bp) + bp por separado para evitar la doble resta de
    # invested que dejaba 56% en cash. Sin margen: tope al equity y al bp real.
    equity_intents = check_capital_headroom(equity, positions, equity_intents, buying_power=bp)
    logger.info(
        "Equity plan: %d órdenes (capital=$%.2f, ya invertido=$%.2f, headroom=$%.2f)",
        len(equity_intents), capital, invested, capital - invested,
    )

    # ── Scale-in: nuevas posiciones entran al 60% ─────────────────────────────
    held_tickers = {p.ticker for p in positions}
    equity_intents = _apply_scale_in(equity_intents, held_tickers)

    # ── Stale signal guard: señales viejas → bloquear BUYs nuevos ──────────────
    if _stale_signals:
        held_set = {p.ticker for p in positions}
        equity_intents = [
            i for i in equity_intents
            if i.side.upper() != "BUY" or i.ticker in held_set
        ]
        logger.warning("Stale signal guard: BUYs en tickers no holding filtrados")

    # ── Validador de signals (Iter4): rechaza signals incoherentes ────────────
    # Bug real del 19/05: MU venía con stop=$682.54 y tp=$650.66 (TP DEBAJO del
    # stop). Trade perdedor garantizado. Validamos AHORA antes de ejecutar.
    # Reglas:
    #   1. stop_loss > 0 y take_profit > 0 (si están definidos)
    #   2. take_profit > stop_loss × 1.5 (R/R mínimo aceptable)
    #   3. take_profit > signal.price (no comprar para vender abajo)
    #   4. stop_loss < signal.price (stop por debajo del precio actual)
    # Si alguna falla, log + alerta + skip.
    _validation_failures = []
    sane_intents = []
    for intent in equity_intents:
        if intent.side.upper() != "BUY":
            sane_intents.append(intent)
            continue

        sig_match = next(
            (s for s in signals.long_term + signals.short_term if s.ticker == intent.ticker),
            None,
        )
        sig_price = float(sig_match.price) if sig_match else 0.0
        sl = intent.stop_loss
        tp = intent.take_profit

        # 1. Stop o TP en 0 o negativo
        if sl is not None and sl <= 0:
            _validation_failures.append(f"{intent.ticker}: stop_loss invalido ({sl})")
            continue
        if tp is not None and tp <= 0:
            _validation_failures.append(f"{intent.ticker}: take_profit invalido ({tp})")
            continue

        # 2. Coherencia stop vs TP — el bug critico de MU
        if sl is not None and tp is not None:
            if tp <= sl:
                _validation_failures.append(
                    f"{intent.ticker}: TP ${tp:.2f} <= SL ${sl:.2f} (TP debajo del stop, trade perdedor)"
                )
                continue
            # R/R minimo 1.5:1
            if sig_price > 0:
                upside = tp - sig_price
                downside = sig_price - sl
                if downside > 0 and upside / downside < 1.5:
                    _validation_failures.append(
                        f"{intent.ticker}: R/R {upside/downside:.2f}x < 1.5x (TP ${tp:.2f} SL ${sl:.2f} px ${sig_price:.2f})"
                    )
                    continue

        # 3. TP debajo del precio (señal stale)
        if tp is not None and sig_price > 0 and tp <= sig_price * 1.01:
            _validation_failures.append(
                f"{intent.ticker}: TP ${tp:.2f} <= precio ${sig_price:.2f} (señal stale o corrupta)"
            )
            continue

        # 4. Stop por encima del precio (BUY con stop arriba = inmediato cierre)
        if sl is not None and sig_price > 0 and sl >= sig_price * 0.999:
            _validation_failures.append(
                f"{intent.ticker}: SL ${sl:.2f} >= precio ${sig_price:.2f} (stop arriba del precio, cierre inmediato)"
            )
            continue

        sane_intents.append(intent)

    if _validation_failures:
        msg = "*VALIDADOR SIGNALS* — rechazadas %d ordenes BUY:\n%s" % (
            len(_validation_failures),
            "\n".join(f"  - {f}" for f in _validation_failures),
        )
        logger.warning("Signal validation rejected %d BUYs:\n%s", len(_validation_failures), "\n".join(_validation_failures))
        try:
            from alpha_agent.notifications import send_notification
            send_notification(msg, header="VALIDADOR SIGNALS")
        except Exception as _ne:
            logger.debug("send_notification fail: %s", _ne)

    equity_intents = sane_intents

    # ── Risk Arbiter: debate bull/bear antes de BUY ──────────────────────────
    equity_intents = _apply_risk_debate(signals, equity_intents, positions, capital_adj)
    logger.info("Risk Arbiter: %d órdenes post-filtro", len(equity_intents))

    # ── Earnings guard: no BUY si el ticker tiene earnings ≤3 días ────────────
    # earnings_guard ya se usaba en scoring (penaliza el score) pero NUNCA se
    # chequeaba al ejecutar. Resultado: si una señal sobrevive el scoring por
    # poco, el trader compra el día previo a earnings y se come el gap overnight.
    try:
        from alpha_agent.analytics.earnings_guard import get_earnings_soon
        _buy_tickers = [i.ticker for i in equity_intents if i.side.upper() == "BUY"]
        if _buy_tickers:
            _earn = get_earnings_soon(_buy_tickers, days=3)
            if _earn:
                _filtered = []
                for intent in equity_intents:
                    if intent.side.upper() == "BUY" and intent.ticker in _earn:
                        logger.warning(
                            "EARNINGS guard: skip BUY %s — earnings %s (≤3d, gap risk overnight)",
                            intent.ticker, _earn[intent.ticker],
                        )
                        continue
                    _filtered.append(intent)
                equity_intents = _filtered
    except Exception as _eg_err:
        logger.debug("earnings_guard no disponible (%s) — siguiente sin filtro", _eg_err)

    # ── Macro calendar guard: evento mayor mañana → CP al 50% ───────────────────
    try:
        from alpha_agent.macro.event_calendar import get_upcoming_events
        upcoming = get_upcoming_events(days_ahead=1)
        if upcoming:
            logger.warning("Macro guard activo — eventos: %s → CP notional x50%%", upcoming)
            guarded = []
            for intent in equity_intents:
                if getattr(intent, "horizon", "") in ("CP", "short_term"):
                    intent = TradeIntent(
                        ticker=intent.ticker,
                        side=intent.side,
                        notional=intent.notional * 0.50,
                        horizon=intent.horizon,
                        stop_loss=intent.stop_loss,
                        take_profit=intent.take_profit,
                    )
                guarded.append(intent)
            equity_intents = guarded
    except Exception as _mcg_exc:
        logger.debug("event_calendar no disponible (%s)", _mcg_exc)
        upcoming = []

    # iter31: ¿hay entradas de NOMBRES NUEVOS en este plan? (no holdeados antes)
    _new_name_entry = any(
        i.side.upper() == "BUY" and i.ticker not in held_tickers
        for i in equity_intents
    )

    fills.extend(_submit_equity_intents(
        broker, equity_intents, dry_run=dry_run,
        regime=regime, vix=vix,
    ))

    # iter31: si se ejecutó (live) una rotación de entradas nuevas, registrar la
    # fecha para cerrar la ventana mensual. El backfill de un libro sub-desplegado
    # también la consume (arranca el hold mensual del libro completo). Las salidas
    # y scale-in NO la tocan (no son nombres nuevos).
    if (not dry_run) and _entry_open and _new_name_entry:
        record_entry_rotation()

    # ── Opciones (direccionales + hedge) ────────────────────────────
    option_intents = build_option_intents(signals, capital_adj)
    logger.info("Options plan: %d intents", len(option_intents))
    opt_fills = _submit_option_intents(broker, option_intents, dry_run=dry_run)
    fills.extend(opt_fills)

    # iter15: fallback opciones → equity. Si NINGUNA opción se desplegó (no se
    # encontró contrato, etc.), el sleeve de opciones quedaría en cash. En perfil
    # agresivo redirigimos ese capital al mejor nombre CP para no dejar plata ociosa.
    try:
        fills.extend(_options_fallback_to_equity(
            broker, signals, opt_fills, capital_adj, dry_run=dry_run,
            regime=regime, vix=vix,
        ))
    except Exception as _ofb_exc:
        logger.debug("options fallback no aplicado (%s)", _ofb_exc)

    return fills


def _options_fallback_to_equity(broker, signals, opt_fills, capital_adj, *,
                                dry_run: bool, regime: str, vix: float) -> list[dict]:
    """Si las opciones no se desplegaron, redirige su presupuesto al top CP.

    Sólo en perfil AGGRESSIVE. Respeta el buying power disponible (no sobre-invierte).
    """
    try:
        from alpha_agent.config import PARAMS as _P
    except Exception:
        return []
    if getattr(_P, "risk_appetite", "") != "AGGRESSIVE":
        return []
    # ¿Se desplegó alguna opción?
    if any(f.get("status") in ("submitted", "filled") for f in (opt_fills or [])):
        return []
    if not signals.short_term:
        return []

    opt_budget = capital_adj * float(getattr(_P, "weight_options", 0.0) or 0.0)
    if opt_budget < 50:
        return []

    # Headroom real: buying power − invertido actual
    try:
        bp = float(broker.get_buying_power()) if hasattr(broker, "get_buying_power") else float(broker.get_equity())
        invested = total_invested_notional(broker.get_positions())
        headroom = max(0.0, bp - invested)
    except Exception:
        headroom = opt_budget
    notional = min(opt_budget, headroom)
    if notional < 50:
        return []

    top = signals.short_term[0]
    logger.info(
        "Options fallback → equity: opciones no desplegadas, redirijo $%.0f al top CP %s",
        notional, top.ticker,
    )
    intent = TradeIntent(
        ticker=top.ticker, side="BUY", notional=notional, horizon="CP",
        stop_loss=top.stop_loss, take_profit=top.take_profit,
    )
    return _submit_equity_intents(broker, [intent], dry_run=dry_run, regime=regime, vix=vix)


def _apply_scale_in(intents: list[TradeIntent], held_tickers: set[str]) -> list[TradeIntent]:
    """
    Nuevas posiciones entran a una fracción del notional (resto se completa el
    ciclo siguiente si confirma). Reduce slippage y riesgo de breakout falso.

    iter15: la fracción depende del perfil de riesgo. En AGGRESSIVE entramos al
    85% (no 60%) — el scale-in al 60% dejaba el libro sub-desplegado (day-1 ~54%)
    y eso rema contra el objetivo de multiplicar rápido. 85% mantiene un pequeño
    colchón contra falsos breakouts sin sacrificar despliegue.
    """
    try:
        from alpha_agent.config import PARAMS as _P
        frac = 0.85 if getattr(_P, "risk_appetite", "") == "AGGRESSIVE" else 0.60
    except Exception:
        frac = 0.60
    result = []
    for intent in intents:
        if intent.side.upper() == "BUY" and intent.ticker not in held_tickers:
            scaled = intent.notional * frac
            logger.info(
                "Scale-in NEW %s: $%.0f → $%.0f (%.0f%% entrada inicial)",
                intent.ticker, intent.notional, scaled, frac * 100,
            )
            intent = TradeIntent(
                ticker=intent.ticker,
                side=intent.side,
                notional=scaled,
                horizon=intent.horizon,
                stop_loss=intent.stop_loss,
                take_profit=intent.take_profit,
            )
        result.append(intent)
    return result


def _apply_risk_debate(
    signals: Signals,
    intents: list[TradeIntent],
    positions: list,
    capital: float,
) -> list[TradeIntent]:
    """
    Filtra y ajusta intents BUY via debate bull/bear (Claude Haiku).
    SELL/SELL_SHORT pasan sin modificación.

    Modo BULL: los trades CP siempre pasan (momentum rápido, latencia de API daña).
    LP BULL: solo llama al arbiter si conviction == BAJA y sentiment negativo.
    Modo BEAR/SIDEWAYS: arbiter completo sin excepción.
    """
    try:
        from alpha_agent.news.claude_analyst import risk_debate
    except ImportError:
        return intents

    signal_lookup: dict[str, Signal] = {
        s.ticker: s for s in signals.long_term + signals.short_term
    }

    macro = signals.macro or {}
    regime = macro.get("regime", "unknown").lower()
    portfolio_ctx = {
        "regime": regime,
        "vix": float((macro.get("prices") or {}).get("vix", 18)),
        "current_positions": [p.ticker for p in positions],
        "capital_usd": capital,
    }

    filtered: list[TradeIntent] = []
    for intent in intents:
        if intent.side.upper() != "BUY":
            filtered.append(intent)
            continue

        sig = signal_lookup.get(intent.ticker)
        if sig is None:
            filtered.append(intent)
            continue

        horizon = getattr(sig, "horizon", intent.horizon or "")

        # ── Modo BULL: arbiter selectivo ─────────────────────────────────────
        if regime == "bull":
            # CP trades: pasan directo (momentum rápido, no frenar con API)
            if horizon == "CP":
                logger.info("Risk Arbiter BYPASS (bull+CP) %s", intent.ticker)
                filtered.append(intent)
                continue

            # LP trades: solo detener si conviction BAJA + sentimiento negativo
            if horizon == "LP":
                conviction = (sig.thesis or {}).get("conviction", "MEDIA")
                sentiment = float(
                    ((sig.thesis or {}).get("fundamental") or {}).get("sentiment_score", 0) or 0
                )
                if not (conviction == "BAJA" and sentiment < -0.3):
                    logger.info("Risk Arbiter PROCEED (bull+LP) %s — conviction=%s", intent.ticker, conviction)
                    filtered.append(intent)
                    continue
                # BAJA + negativo → sí pasa por el debate completo

        # ── Debate completo (BEAR/SIDEWAYS o LP BAJA en BULL) ────────────────
        debate = risk_debate(
            ticker=intent.ticker,
            signal={
                "price": sig.price,
                "stop_loss": sig.stop_loss,
                "take_profit": sig.take_profit,
                "thesis": sig.thesis or {},
            },
            portfolio_context=portfolio_ctx,
        )

        verdict = debate.get("verdict", "PROCEED")
        size_adj = max(0.0, min(1.0, float(debate.get("size_adjustment", 1.0))))

        if verdict == "SKIP":
            logger.warning(
                "Risk Arbiter SKIP %s — %s",
                intent.ticker, debate.get("bear_case", "?"),
            )
            continue

        if verdict == "REDUCE_SIZE" and size_adj < 1.0:
            new_notional = intent.notional * size_adj
            logger.info(
                "Risk Arbiter REDUCE_SIZE %s: $%.0f→$%.0f (%.0f%%) — %s",
                intent.ticker, intent.notional, new_notional, size_adj * 100,
                debate.get("bear_case", ""),
            )
            intent = TradeIntent(
                ticker=intent.ticker,
                side=intent.side,
                notional=new_notional,
                horizon=intent.horizon,
                stop_loss=intent.stop_loss,
                take_profit=intent.take_profit,
            )
        else:
            logger.info(
                "Risk Arbiter PROCEED %s — %s",
                intent.ticker, debate.get("bull_case", ""),
            )

        filtered.append(intent)

    return filtered


def _limit_price(price: float, side: str) -> float:
    """
    Precio límite para reducir slippage vs market orders.
    BUY: 0.15% sobre el mid → casi siempre se ejecuta en segundos.
    SELL: 0.15% bajo el mid.
    Esto captura mejor spread en stocks poco líquidos (VIST, YPF, AVAV).
    """
    if side.upper() in ("BUY",):
        return round(price * 1.0015, 2)
    return round(price * 0.9985, 2)


def _submit_equity_intents(
    broker: BrokerBase,
    intents: list[TradeIntent],
    *,
    dry_run: bool,
    regime: str = "unknown",
    vix: float = 18.0,
) -> list[dict]:
    fills = []
    for intent in intents:
        try:
            price = broker.get_last_price(intent.ticker)
            qty = round(intent.notional / price, 4) if price > 0 else 0
            if qty <= 0:
                logger.warning("Skip %s: qty = 0", intent.ticker)
                continue

            lp = _limit_price(price, intent.side)
            order = Order(
                ticker=intent.ticker,
                side=intent.side,
                qty=qty,
                order_type="limit",
                limit_price=lp,
                stop_loss=intent.stop_loss,
                take_profit=intent.take_profit,
                client_order_id=f"alpha_eq_{datetime.now().strftime('%Y%m%d%H%M%S')}_{intent.ticker}",
            )

            if dry_run:
                logger.info("[DRY RUN EQUITY] %s @ limit $%.2f (mid $%.2f)", order, lp, price)
                fills.append({
                    "asset": "equity", "ticker": intent.ticker, "side": intent.side,
                    "qty": qty, "status": "dry_run", "order_id": None,
                    "limit_price": lp,
                })
            else:
                oid = broker.submit_order(order)
                fills.append({
                    "asset": "equity", "ticker": intent.ticker, "side": intent.side,
                    "qty": qty, "status": "submitted", "order_id": oid,
                    "limit_price": lp,
                })
                try:
                    from alpha_agent.analytics.trade_db import log_trade, log_trade_close, get_trades as _gt
                    log_trade(
                        ticker=intent.ticker,
                        side=intent.side,
                        qty=qty,
                        price=price,
                        notional=intent.notional,
                        sleeve=intent.horizon,
                        status="submitted",
                        order_id=str(oid) if oid else None,
                        stop_loss=intent.stop_loss,
                        take_profit=intent.take_profit,
                        regime=regime,
                        vix=vix,
                        limit_price=lp,
                    )
                    # For SELLs: also close the matching open BUY so P&L is tracked immediately
                    if intent.side.upper() == "SELL":
                        open_buys = [t for t in _gt(ticker=intent.ticker, limit=5)
                                     if t["side"] == "BUY" and not t.get("closed_at")]
                        if open_buys:
                            buy_p = open_buys[0]["price"] or price
                            pnl_usd = round((price - buy_p) * qty, 2)
                            pnl_pct = round((price / buy_p - 1) * 100, 2) if buy_p else 0.0
                            log_trade_close(ticker=intent.ticker, exit_price=price,
                                            pnl_usd=pnl_usd, pnl_pct=pnl_pct)
                except Exception as _db_exc:
                    logger.debug("trade_db log error: %s", _db_exc)
        except Exception as e:
            msg = str(e).lower()
            # iter20: PDT (Pattern Day Trading) — cuenta <$25k no puede hacer >3
            # day-trades/5d. Alpaca bloquea el SELL. NO es un error del sistema:
            # lo logueamos como info y la posición sigue (el monitor la gestiona
            # con stop/trailing). Evita el spam de ERROR y respeta la regla.
            if "pattern day trading" in msg or "40310100" in msg:
                logger.info(
                    "PDT: %s %s diferido (límite day-trade <$25k) — la posición sigue, "
                    "el monitor la gestiona", intent.side, intent.ticker,
                )
                fills.append({
                    "asset": "equity", "ticker": intent.ticker, "side": intent.side,
                    "qty": 0, "status": "pdt_deferred", "order_id": None,
                })
            else:
                logger.error("Falló equity %s: %s", intent.ticker, e)
                fills.append({
                    "asset": "equity", "ticker": intent.ticker, "side": intent.side,
                    "qty": 0, "status": f"error: {e}", "order_id": None,
                })
    return fills


def _submit_option_intents(broker: BrokerBase, intents: list[OptionIntent], *, dry_run: bool) -> list[dict]:
    fills = []
    for intent in intents:
        try:
            opt_order = OptionOrder(
                underlying=intent.underlying,
                option_type=intent.option_type,
                target_strike=intent.target_strike,
                target_expiry=intent.target_expiry,
                contracts=intent.contracts,
                side="BUY",
                order_type="market",
                client_order_id=f"alpha_opt_{datetime.now().strftime('%Y%m%d%H%M%S')}_{intent.underlying}",
            )

            if dry_run:
                logger.info(
                    "[DRY RUN OPT] BUY %d x %s %s @ strike %.2f exp %s (est $%.0f)",
                    intent.contracts, intent.underlying, intent.option_type,
                    intent.target_strike, intent.target_expiry, intent.contract_cost_est,
                )
                fills.append({
                    "asset": "option", "underlying": intent.underlying,
                    "type": intent.option_type, "contracts": intent.contracts,
                    "strike": intent.target_strike, "expiry": intent.target_expiry,
                    "role": intent.role, "status": "dry_run", "order_id": None,
                })
            else:
                try:
                    oid = broker.submit_option_order(opt_order)
                    fills.append({
                        "asset": "option", "underlying": intent.underlying,
                        "type": intent.option_type, "contracts": intent.contracts,
                        "strike": intent.target_strike, "expiry": intent.target_expiry,
                        "role": intent.role, "status": "submitted", "order_id": oid,
                    })
                except NotImplementedError as e:
                    logger.warning("Broker no soporta opciones todavía: %s", e)
                    fills.append({
                        "asset": "option", "underlying": intent.underlying,
                        "type": intent.option_type, "contracts": intent.contracts,
                        "status": "unsupported", "order_id": None,
                    })
        except Exception as e:
            logger.error("Falló option %s: %s", intent.underlying, e)
            fills.append({
                "asset": "option", "underlying": intent.underlying,
                "type": intent.option_type, "contracts": intent.contracts,
                "status": f"error: {e}", "order_id": None,
            })
    return fills


def summarize_fills(fills: list[dict]) -> str:
    if not fills:
        return "Sin órdenes ejecutadas."
    meta = next((f for f in fills if f.get("status") == "meta"), {})
    vix = meta.get("vix")
    vix_mult = meta.get("vix_mult")
    real_fills = [f for f in fills if f.get("status") != "meta"]
    header = f"*EJECUTOR* — {len(real_fills)} ordenes/intents"
    if vix is not None and vix_mult is not None and vix_mult != 1.0:
        direction = "reducido" if vix_mult < 1 else "ampliado"
        header += f" | VIX {vix:.0f} → sizing {direction} al {vix_mult*100:.0f}%"
    regime = meta.get("regime", "unknown")
    regime_mult = meta.get("regime_mult", 1.0)
    if regime_mult != 1.0:
        direction = "ampliado" if regime_mult > 1 else "reducido"
        header += f" | {regime.upper()} → capital {direction} al {regime_mult*100:.0f}%"
    lines = [header]
    fills = real_fills
    for f in fills:
        if f.get("status") == "kill_switch":
            lines.append(f"🛑 {f.get('reason')}")
            continue
        if f.get("status") == "market_closed":
            lines.append("⏸ Mercado cerrado, no se operó.")
            continue
        if f.get("asset") == "option":
            lines.append(
                f"• {f.get('type','').upper()} {f.get('underlying')} x{f.get('contracts')} "
                f"strike {f.get('strike')} exp {f.get('expiry')} [{f.get('role')}] — {f.get('status')}"
            )
        else:
            lines.append(
                f"• {f.get('side')} {f.get('qty')} {f.get('ticker')} — {f.get('status')}"
            )
    return "\n".join(lines)


def notify(fills: list[dict]) -> None:
    send_whatsapp(summarize_fills(fills), header="EJECUTOR ALPHA")
