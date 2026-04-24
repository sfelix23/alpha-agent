"""
Análisis fundamental tipo Wall Street para cada activo del universo.

Extrae métricas clave de yfinance y genera un contexto de valuación
que Claude usa para producir una tesis de inversión completa.

Funciones públicas:
    get_fundamentals(ticker) → dict con P/E, FCF yield, ROE, etc.
    format_for_claude(ticker, fundamentals) → str formateado para el prompt
"""

from __future__ import annotations

import logging
from functools import lru_cache

import pandas as pd

logger = logging.getLogger(__name__)

# Medias sectoriales aproximadas (P/E forward) para contexto relativo
_SECTOR_PE: dict[str, float] = {
    "Tech": 28.0,
    "Healthcare": 22.0,
    "Energy": 12.0,
    "Defense": 18.0,
    "Materials": 15.0,
    "Financials": 13.0,
    "Consumer": 20.0,
    "ETF": 20.0,
    "Crypto": 0.0,
    "RealEstate": 25.0,
    "Other": 18.0,
}


@lru_cache(maxsize=64)
def get_fundamentals(ticker: str) -> dict:
    """
    Descarga métricas fundamentales de yfinance para un ticker.
    Resultado cacheado por proceso (TTL implícito de 1 run).

    Devuelve dict con claves:
        pe_trailing, pe_forward, pb, ev_ebitda, fcf_yield,
        revenue_growth_yoy, roe, debt_equity, insider_pct,
        analyst_rating, n_analysts, market_cap_b,
        pct_52w_high, dividend_yield, sector
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.info or {}

        market_cap = info.get("marketCap") or 0
        fcf = info.get("freeCashflow") or 0
        fcf_yield = (fcf / market_cap * 100) if market_cap > 0 else None

        # Consensus analyst rating (1=Strong Buy, 5=Strong Sell) → invertir escala
        analyst_rating = None
        n_analysts = 0
        try:
            recs = t.recommendations
            if recs is not None and not recs.empty:
                # tomar últimas 3 meses
                recent = recs.tail(10)
                # columna "To Grade" tiene strings: Buy, Hold, Sell etc.
                grade_col = "To Grade" if "To Grade" in recent.columns else None
                if grade_col:
                    grade_map = {
                        "Strong Buy": 5, "Buy": 4, "Outperform": 4,
                        "Hold": 3, "Neutral": 3, "Market Perform": 3,
                        "Underperform": 2, "Sell": 1, "Strong Sell": 1,
                    }
                    scores = [grade_map.get(g, 3) for g in recent[grade_col] if g]
                    if scores:
                        analyst_rating = round(sum(scores) / len(scores), 1)
                        n_analysts = len(scores)
        except Exception:
            pass

        # % desde el máximo de 52 semanas
        high_52 = info.get("fiftyTwoWeekHigh")
        current = info.get("currentPrice") or info.get("regularMarketPrice")
        pct_52w = None
        if high_52 and current and high_52 > 0:
            pct_52w = round((current - high_52) / high_52 * 100, 1)

        return {
            "pe_trailing": info.get("trailingPE"),
            "pe_forward": info.get("forwardPE"),
            "pb": info.get("priceToBook"),
            "ev_ebitda": info.get("enterpriseToEbitda"),
            "fcf_yield": round(fcf_yield, 2) if fcf_yield is not None else None,
            "revenue_growth_yoy": round((info.get("revenueGrowth") or 0) * 100, 1),
            "roe": round((info.get("returnOnEquity") or 0) * 100, 1),
            "debt_equity": round(info.get("debtToEquity") or 0, 1),
            "insider_pct": round((info.get("heldPercentInsiders") or 0) * 100, 1),
            "analyst_rating": analyst_rating,
            "n_analysts": n_analysts,
            "market_cap_b": round(market_cap / 1e9, 1) if market_cap else None,
            "pct_52w_high": pct_52w,
            "dividend_yield": round((info.get("dividendYield") or 0) * 100, 2),
            "sector": info.get("sector", "Other"),
            "long_name": info.get("longName", ticker),
        }

    except Exception as e:
        logger.warning("get_fundamentals(%s): %s", ticker, e)
        return {"sector": "Other", "long_name": ticker}


@lru_cache(maxsize=64)
def get_recent_upgrades(ticker: str, days: int = 7) -> dict:
    """
    Detecta upgrades/downgrades de analistas en los últimos N días.

    Returns:
        dict con claves:
            'recent_upgrade': bool — al menos un upgrade en el período
            'recent_downgrade': bool — al menos un downgrade en el período
            'firms': list[str] — firmas que actuaron recientemente
    """
    try:
        import yfinance as yf
        from datetime import datetime, timedelta, timezone

        ud = yf.Ticker(ticker).upgrades_downgrades
        if ud is None or ud.empty:
            return {"recent_upgrade": False, "recent_downgrade": False, "firms": []}

        idx = ud.index
        if hasattr(idx, "tz"):
            if idx.tz is None:
                idx = idx.tz_localize("UTC")
        else:
            idx = pd.to_datetime(ud.index, utc=True)

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        recent = ud[idx >= cutoff]
        if recent.empty:
            return {"recent_upgrade": False, "recent_downgrade": False, "firms": []}

        upgrade_grades = {"Strong Buy", "Buy", "Outperform", "Overweight", "Top Pick", "Positive", "Add"}
        downgrade_grades = {"Strong Sell", "Sell", "Underperform", "Underweight", "Reduce", "Negative"}

        grade_col = next((c for c in ("ToGrade", "To Grade") if c in recent.columns), None)
        recent_upgrade = recent_downgrade = False
        if grade_col:
            grades = set(recent[grade_col].dropna())
            recent_upgrade = bool(grades & upgrade_grades)
            recent_downgrade = bool(grades & downgrade_grades)
        else:
            action_col = next((c for c in ("Action",) if c in recent.columns), None)
            if action_col:
                actions = recent[action_col].str.lower()
                recent_upgrade = actions.isin(["up", "init"]).any()
                recent_downgrade = actions.isin(["down"]).any()

        firm_col = next((c for c in ("Firm",) if c in recent.columns), None)
        firms = recent[firm_col].dropna().tolist()[:3] if firm_col else []

        if recent_upgrade or recent_downgrade:
            logger.debug(
                "get_recent_upgrades(%s): upgrade=%s downgrade=%s firms=%s",
                ticker, recent_upgrade, recent_downgrade, firms,
            )
        return {"recent_upgrade": recent_upgrade, "recent_downgrade": recent_downgrade, "firms": firms}

    except Exception as e:
        logger.debug("get_recent_upgrades(%s): %s", ticker, e)
        return {"recent_upgrade": False, "recent_downgrade": False, "firms": []}


def format_for_claude(ticker: str, f: dict, *, quant: dict | None = None) -> str:
    """
    Formatea los datos fundamentales + quant para el prompt de Claude.

    Args:
        ticker: símbolo del activo
        f: resultado de get_fundamentals()
        quant: dict opcional con claves del CAPM (beta, alpha_jensen, sharpe, etc.)

    Returns:
        Bloque de texto formateado para insertar en un prompt de Claude.
    """
    sector = f.get("sector", "Other")
    sector_pe = _SECTOR_PE.get(sector, 18.0)

    def _fmt(v, fmt=".1f", suffix="") -> str:
        if v is None:
            return "N/D"
        return f"{v:{fmt}}{suffix}"

    pe_fwd = f.get("pe_forward")
    pe_vs = ""
    if pe_fwd and sector_pe > 0:
        diff = pe_fwd - sector_pe
        pe_vs = f" (sector: {sector_pe:.0f}x, {'PREMIUM' if diff > 0 else 'DESCUENTO'} {abs(diff):.0f}x)"

    rating_str = "N/D"
    if f.get("analyst_rating"):
        labels = {5:"Strong Buy", 4:"Buy", 3:"Hold", 2:"Underperform", 1:"Sell"}
        r = f["analyst_rating"]
        label = labels.get(round(r), "Hold")
        rating_str = f"{label} ({r:.1f}/5, {f.get('n_analysts',0)} analistas)"

    quant_block = ""
    if quant:
        beta = quant.get("beta", 0) or 0
        alpha = (quant.get("alpha_jensen", 0) or 0) * 100
        sharpe = quant.get("sharpe", 0) or 0
        rsi = quant.get("rsi", 50) or 50
        ret_1m = (quant.get("ret_1m", 0) or 0) * 100
        ret_3m = (quant.get("ret_3m", 0) or 0) * 100
        quant_block = f"""
MOMENTUM Y QUANT:
- Beta: {beta:.2f} | Alpha Jensen (anual): {alpha:+.1f}% | Sharpe: {sharpe:.2f}
- RSI(14): {rsi:.0f} | Retorno 1m: {ret_1m:+.1f}% | Retorno 3m: {ret_3m:+.1f}%
- Distancia del max 52W: {_fmt(f.get('pct_52w_high'), '+.1f', '%')}"""

    return f"""ANALISIS FUNDAMENTAL — {ticker} ({f.get('long_name', ticker)})
Sector: {sector} | Market Cap: {_fmt(f.get('market_cap_b'), '.1f', 'B USD')}

VALUACION:
- P/E trailing: {_fmt(f.get('pe_trailing'), '.1f', 'x')} | P/E forward: {_fmt(pe_fwd, '.1f', 'x')}{pe_vs}
- P/Book: {_fmt(f.get('pb'), '.1f', 'x')} | EV/EBITDA: {_fmt(f.get('ev_ebitda'), '.1f', 'x')}
- FCF Yield: {_fmt(f.get('fcf_yield'), '.1f', '%')} | Dividendo: {_fmt(f.get('dividend_yield'), '.2f', '%')}

CALIDAD DEL NEGOCIO:
- Crecimiento revenue YoY: {_fmt(f.get('revenue_growth_yoy'), '.1f', '%')}
- ROE: {_fmt(f.get('roe'), '.1f', '%')} | Deuda/Equity: {_fmt(f.get('debt_equity'), '.1f', 'x')}
- Insiders: {_fmt(f.get('insider_pct'), '.1f', '%')} | Consenso: {rating_str}
{quant_block}"""
