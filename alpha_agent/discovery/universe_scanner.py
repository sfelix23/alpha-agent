"""
Discovery Agent — escanea el S&P 500 + sector leaders en busca de
oportunidades fuera del universo actual de 51 activos.

Corre una vez por semana junto con el rebalancer (viernes).

Flujo:
1. Descarga lista S&P 500 desde Wikipedia
2. Filtra los tickers que ya están en el universo
3. Calcula Sharpe 3M y momentum 1M para cada candidato
4. Pasa los top-N a Claude para síntesis y priorización
5. Guarda resultado en signals/discovery.json
6. Si un ticker aparece 2 semanas consecutivas → alerta especial

Uso:
    from alpha_agent.discovery import scan_for_candidates
    result = scan_for_candidates()  # devuelve dict con candidatos
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR       = Path(__file__).resolve().parents[2]
DISCOVERY_PATH = BASE_DIR / "signals" / "discovery.json"
N_CANDIDATES   = 12   # top-N por Sharpe antes de pasarlos a Claude
N_FINAL        = 5    # top-N que Claude selecciona para notificar


def _get_sp500_tickers() -> list[str]:
    """Descarga la lista del S&P 500. iter32: Wikipedia 403ea sin User-Agent (pandas
    read_html directo falla) → bajamos el HTML con header de browser y, si falla,
    probamos un CSV público en GitHub. Último recurso: fallback hardcodeado amplio."""
    import pandas as pd

    # Fuente 1: Wikipedia con User-Agent (read_html directo da 403)
    try:
        import io
        import urllib.request
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; AlphaAgent/1.0)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        tables = pd.read_html(io.StringIO(html))
        df = tables[0]
        tickers = df["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
        if len(tickers) >= 400:
            logger.info("S&P 500: %d tickers descargados (Wikipedia)", len(tickers))
            return tickers
    except Exception as e:
        logger.warning("S&P 500 Wikipedia falló (%s) — probando GitHub", e)

    # Fuente 2: CSV público en GitHub (datasets/s-and-p-500-companies)
    try:
        csv_url = ("https://raw.githubusercontent.com/datasets/"
                   "s-and-p-500-companies/main/data/constituents.csv")
        df = pd.read_csv(csv_url)
        col = "Symbol" if "Symbol" in df.columns else df.columns[0]
        tickers = df[col].astype(str).str.replace(".", "-", regex=False).tolist()
        if len(tickers) >= 400:
            logger.info("S&P 500: %d tickers descargados (GitHub CSV)", len(tickers))
            return tickers
    except Exception as e:
        logger.warning("S&P 500 GitHub CSV falló (%s) — usando fallback hardcodeado", e)

    # Fallback: lista amplia diversa de large-caps (no solo ACTIVOS) para que el
    # backtest --broad siga siendo razonablemente diverso aún sin red.
    return [
        "BRK-B", "JNJ", "UNH", "PG", "KO", "PEP", "WMT", "HD", "DIS", "MCD",
        "NFLX", "ADBE", "CRM", "INTC", "QCOM", "MU", "TXN", "AVGO", "ORCL", "IBM",
        "GE", "CAT", "DE", "HON", "MMM", "UPS", "FDX", "LMT", "RTX", "BA",
        "GS", "MS", "BAC", "WFC", "C", "AXP", "BLK", "SCHW", "SPGI",
        "PFE", "MRK", "ABBV", "BMY", "GILD", "TMO", "DHR", "ABT", "CVS",
        "NEE", "D", "SO", "DUK", "XOM", "CVX", "COP", "SLB",
        "PM", "MO", "CL", "KMB", "GIS", "TGT", "LOW", "SBUX", "NKE",
        "T", "VZ", "CMCSA", "CSCO", "ACN", "NOW", "INTU", "AMAT", "LRCX",
    ]


def _quick_score(ticker: str) -> dict | None:
    """
    Calcula Sharpe 3M, momentum 1M y avg dollar volume para un ticker.
    iter17: aplica gate de liquidez — devuelve None si el candidato no es tradeable
    (avg dollar volume < PARAMS.discovery_min_adv_usd o precio < discovery_min_price).
    Sin liquidez, el spread se come el edge en una cuenta chica.
    """
    try:
        import yfinance as yf
        from alpha_agent.config import PARAMS
        data = yf.download(ticker, period="3mo", progress=False, auto_adjust=True)
        if data is None or len(data) < 30:
            return None
        closes = data["Close"].dropna()
        if len(closes) < 20:
            return None
        price = float(closes.iloc[-1])

        # ── Gate de liquidez ──────────────────────────────────────────────
        if price < float(getattr(PARAMS, "discovery_min_price", 5.0)):
            return None
        avg_dollar_vol = 0.0
        try:
            vol = data["Volume"].dropna()
            n = min(20, len(vol), len(closes))
            if n > 0:
                avg_dollar_vol = float((closes.iloc[-n:].values * vol.iloc[-n:].values).mean())
        except Exception:
            avg_dollar_vol = 0.0
        if avg_dollar_vol < float(getattr(PARAMS, "discovery_min_adv_usd", 20_000_000)):
            return None

        rets = closes.pct_change().dropna()
        mean_r = float(rets.mean())
        std_r  = float(rets.std())
        if std_r <= 0:
            return None
        sharpe = (mean_r / std_r) * sqrt(252)
        mom_1m = float((closes.iloc[-1] - closes.iloc[-21]) / closes.iloc[-21]) if len(closes) >= 21 else 0.0
        return {
            "ticker": ticker,
            "sharpe_3m": round(sharpe, 2),
            "mom_1m_pct": round(mom_1m * 100, 1),
            "price": round(price, 2),
            "avg_dollar_vol": round(avg_dollar_vol, 0),
            "combined_score": round(sharpe + mom_1m, 3),  # iter17: para comparar en rotación
        }
    except Exception:
        return None


def _claude_synthesize(candidates: list[dict], macro_context: dict) -> list[dict]:
    """
    Síntesis de candidatos vía Gemini Flash (gratuito) con fallback a Claude Haiku.
    """
    google_key = os.getenv("GOOGLE_API_KEY")
    try:
        if google_key:
            import google.generativeai as genai  # type: ignore
            genai.configure(api_key=google_key)
            _gemini_model = genai.GenerativeModel("gemini-2.0-flash-exp")
        else:
            _gemini_model = None
    except Exception:
        _gemini_model = None

    try:
        import anthropic
        from alpha_agent.config import CLAUDE_MODEL

        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

        regime = macro_context.get("regime", "unknown")
        vix    = macro_context.get("vix", 20)

        # Tabla de candidatos para el prompt
        tabla = "\n".join(
            f"  {c['ticker']}: Sharpe3M={c['sharpe_3m']:.2f} | Mom1M={c['mom_1m_pct']:+.1f}% | Precio=${c['price']:.2f}"
            for c in candidates
        )

        prompt = f"""Sos un portfolio manager senior buscando nuevas ideas de inversión para agregar al universo de seguimiento.

Regimen de mercado actual: {regime.upper()} | VIX: {vix:.1f}

Candidatos detectados por momentum y Sharpe (3 meses):
{tabla}

Para cada candidato evaluá:
1. ¿El momentum es sostenible o es una trampa de valor?
2. ¿Complementa un portfolio con exposición a Tech/Defensa/Energia/Argentina?
3. ¿Es el momento adecuado dado el régimen {regime}?

Respondé con JSON válido (solo el JSON, sin explicación):
{{
  "top_picks": [
    {{
      "ticker": "XXX",
      "prioridad": "ALTA|MEDIA|BAJA",
      "razon": "2-3 líneas de análisis concreto",
      "riesgo": "principal riesgo en 1 línea"
    }}
  ],
  "resumen": "2-3 líneas sobre el contexto general de estas ideas"
}}

Incluí máximo {N_FINAL} picks. Solo los que genuinamente merezcan atención."""

        def _parse_picks(text: str) -> list | None:
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            try:
                return json.loads(text).get("top_picks", [])
            except Exception:
                return None

        # Gemini Flash primero
        if _gemini_model:
            try:
                resp_g = _gemini_model.generate_content(prompt)
                picks = _parse_picks(resp_g.text.strip())
                if picks is not None:
                    return picks
            except Exception as exc_g:
                logger.debug("Gemini scanner synthesis failed: %s", exc_g)

        # Iter3: Fallback Claude Haiku SOLO si flag ON (anti-flag de cuenta)
        from alpha_agent.config import LLM as _LLM
        if not _LLM.enable_anthropic:
            logger.info("scanner synthesis: Gemini fallo y Anthropic OFF → heuristica Sharpe")
            return [
                {"ticker": c["ticker"], "prioridad": "MEDIA",
                 "razon": f"Sharpe 3M: {c['sharpe_3m']:.2f}, momentum 1M: {c['mom_1m_pct']:+.1f}%",
                 "riesgo": "Sin AI disponible (Anthropic flag OFF)"}
                for c in candidates[:N_FINAL]
            ]
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        picks = _parse_picks(resp.content[0].text.strip())
        return picks if picks is not None else []

    except Exception as e:
        logger.warning("AI synthesis falló: %s — usando ranking por Sharpe", e)
        return [
            {"ticker": c["ticker"], "prioridad": "MEDIA",
             "razon": f"Sharpe 3M: {c['sharpe_3m']:.2f}, momentum 1M: {c['mom_1m_pct']:+.1f}%",
             "riesgo": "Sin análisis AI disponible"}
            for c in candidates[:N_FINAL]
        ]


def _load_previous() -> dict:
    """Carga el discovery de la semana anterior para detectar repeticiones."""
    if DISCOVERY_PATH.exists():
        try:
            return json.loads(DISCOVERY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def scan_for_candidates(macro_context: dict | None = None) -> dict:
    """
    Punto de entrada principal. Escanea el mercado y devuelve candidatos.

    Args:
        macro_context: dict con keys 'regime', 'vix' (opcional, se usa para el prompt Claude)

    Returns:
        dict con 'candidates', 'repeated_alerts', 'generated_at', 'summary'
    """
    if macro_context is None:
        macro_context = {}

    from alpha_agent.config import ACTIVOS

    current_universe = set(ACTIVOS.values())
    all_tickers = _get_sp500_tickers()
    to_scan = [t for t in all_tickers if t not in current_universe]
    logger.info("Escaneando %d candidatos fuera del universo...", len(to_scan))

    # Scoring rápido — limitar a 300 para no tardar demasiado
    scored = []
    errors = 0
    for ticker in to_scan[:300]:
        result = _quick_score(ticker)
        if result:
            scored.append(result)
        else:
            errors += 1

    logger.info("Scoring completo: %d válidos, %d errores", len(scored), errors)

    if not scored:
        return {"candidates": [], "repeated_alerts": [], "generated_at": datetime.now(timezone.utc).isoformat()}

    # Top-N por Sharpe para pasar a Claude
    top_n = sorted(scored, key=lambda x: x["sharpe_3m"], reverse=True)[:N_CANDIDATES]
    logger.info("Top %d candidatos: %s", N_CANDIDATES, [c["ticker"] for c in top_n])

    # Claude synthesize
    top_picks = _claude_synthesize(top_n, macro_context)

    # Detectar repeticiones (aparece 2 semanas seguidas)
    prev = _load_previous()
    prev_tickers = {c.get("ticker") for c in prev.get("candidates", [])}
    repeated = []
    for pick in top_picks:
        if pick["ticker"] in prev_tickers:
            repeated.append(pick["ticker"])
            logger.info("⚠️ Ticker repetido por 2ª semana: %s", pick["ticker"])

    result = {
        "candidates": top_picks,
        "repeated_alerts": repeated,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_scanned": len(scored),
        "regime": macro_context.get("regime", "unknown"),
    }

    DISCOVERY_PATH.parent.mkdir(exist_ok=True)
    DISCOVERY_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Discovery guardado: %d picks, %d repetidos", len(top_picks), len(repeated))

    return result


_OVERRIDES_PATH = BASE_DIR / "signals" / "cp_universe_overrides.json"


def _load_overrides() -> dict:
    base = {"added": [], "removed": [], "vetoed": [], "history": [], "last_swap_week": None}
    if _OVERRIDES_PATH.exists():
        try:
            data = json.loads(_OVERRIDES_PATH.read_text(encoding="utf-8"))
            for k, v in base.items():
                data.setdefault(k, v)
            return data
        except Exception as e:
            logger.warning("cp_universe_overrides corrupto (%s) — reset", e)
    return base


def _save_overrides(data: dict) -> None:
    _OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _OVERRIDES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_OVERRIDES_PATH)


def _rotate_universe(disc_result: dict, broker=None) -> str:
    """Rotación automática del CP_UNIVERSE con guardarraíles (iter17).

    Si un candidato de discovery repite 2 semanas, pasa el gate de liquidez y supera
    claramente (margen) al miembro más flojo del universo efectivo, lo INCORPORA y saca
    al flojo. Guardarraíles: 1 swap/semana, nunca saca posiciones abiertas ni el núcleo
    protegido, respeta la lista de veto. Devuelve un resumen para WhatsApp (o "").
    """
    from alpha_agent.config import (
        PARAMS, PROTECTED_CP, get_effective_cp_universe,
    )
    if not getattr(PARAMS, "rotation_enabled", False):
        return ""

    overrides = _load_overrides()
    vetoed = set(overrides.get("vetoed", []))
    week = datetime.now(timezone.utc).strftime("%Y-W%U")
    if overrides.get("last_swap_week") == week:
        logger.info("Rotación: ya hubo swap esta semana (%s) — skip", week)
        return ""

    # 1. Candidatos elegibles: repetidos 2da semana, no vetados
    repeated = [t for t in disc_result.get("repeated_alerts", []) if t not in vetoed]
    if not repeated:
        return ""

    effective = set(get_effective_cp_universe())

    # 2. Posiciones abiertas (nunca se sacan)
    open_tickers: set[str] = set()
    if broker is not None:
        try:
            open_tickers = {p.ticker for p in broker.get_positions()}
        except Exception as e:
            logger.debug("rotación: no pude leer posiciones (%s)", e)

    # 3. Scorear candidatos elegibles (re-aplica gate de liquidez)
    cand_scores = []
    for t in repeated:
        if t in effective:
            continue
        s = _quick_score(t)
        if s:
            cand_scores.append(s)
    if not cand_scores:
        logger.info("Rotación: ningún candidato repetido pasó el gate de liquidez")
        return ""
    best = max(cand_scores, key=lambda x: x["combined_score"])

    # 4. Scorear miembros del universo efectivo (excluye protegidos + posiciones abiertas)
    removable = [t for t in effective if t not in PROTECTED_CP and t not in open_tickers]
    member_scores = []
    for t in removable:
        s = _quick_score(t)
        if s:
            member_scores.append(s)
    if not member_scores:
        logger.info("Rotación: no hay miembros removibles (todos protegidos/posición abierta)")
        return ""
    weakest = min(member_scores, key=lambda x: x["combined_score"])

    # 5. Promoción sólo si el candidato supera al más flojo por el margen
    margin = float(getattr(PARAMS, "rotation_margin", 0.20))
    threshold = weakest["combined_score"] * (1 + margin) if weakest["combined_score"] > 0 else 0.0
    if best["combined_score"] <= threshold:
        logger.info(
            "Rotación: %s (score %.2f) no supera a %s (score %.2f × %.0f%%) — sin swap",
            best["ticker"], best["combined_score"], weakest["ticker"],
            weakest["combined_score"], (1 + margin) * 100,
        )
        return ""

    # 6. Ejecutar swap (override persistido)
    overrides.setdefault("added", []).append(best["ticker"])
    overrides.setdefault("removed", []).append(weakest["ticker"])
    overrides["last_swap_week"] = week
    overrides.setdefault("history", []).append({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "in": best["ticker"], "out": weakest["ticker"],
        "in_score": best["combined_score"], "out_score": weakest["combined_score"],
    })
    _save_overrides(overrides)

    msg = (
        f"\n🔄 *ROTACIÓN AUTOMÁTICA DE UNIVERSO*\n"
        f"+ {best['ticker']} (Sharpe3M {best['sharpe_3m']:.2f}, mom1M {best['mom_1m_pct']:+.1f}%)\n"
        f"− {weakest['ticker']} (Sharpe3M {weakest['sharpe_3m']:.2f}, mom1M {weakest['mom_1m_pct']:+.1f}%)\n"
        f"Para revertir: *veto {best['ticker']}*"
    )
    logger.info("ROTACIÓN: +%s / -%s", best["ticker"], weakest["ticker"])
    return msg


def format_whatsapp_discovery(result: dict) -> str:
    """Formatea el resultado del discovery para el brief de WhatsApp del viernes."""
    candidates = result.get("candidates", [])
    repeated   = result.get("repeated_alerts", [])

    if not candidates:
        return ""

    lines = ["\n🔍 *NUEVAS IDEAS* (fuera del universo actual):"]
    for c in candidates:
        prio_emoji = {"ALTA": "🔥", "MEDIA": "📌", "BAJA": "💡"}.get(c.get("prioridad",""), "📌")
        lines.append(f"{prio_emoji} *{c['ticker']}*: {c.get('razon','')[:80]}")

    if repeated:
        lines.append(f"\n⚠️ *2DA SEMANA CONSECUTIVA*: {', '.join(repeated)} — considerar incorporar al universo")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# iter50 — OPPORTUNITY RADAR (read-only, NO alimenta rotación ni trading)
# ════════════════════════════════════════════════════════════════════════════

OPPORTUNITIES_PATH = BASE_DIR / "signals" / "opportunities.json"

# iter53 — ETFs sectoriales + temáticos para detección de ROTACIÓN a nivel tema
# (no solo nombres individuales). Read-only, surface en el radar.
_RADAR_ETFS = [
    # Sectores SPDR (de dónde rota el dinero)
    "XLK", "XLE", "XLF", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
    # Temáticos / industria (semis, IA, defensa, uranio, litio, biotech, fintech)
    "SMH", "SOXX", "ARKK", "ITA", "XAR", "URA", "LIT", "XBI", "FINX", "IGV",
    "TAN", "ICLN", "GDX", "XME", "KWEB", "IBB", "HACK", "BOTZ", "DRIV",
]


def _rsi(series, period: int = 14) -> float:
    """RSI simple sobre una serie de cierres."""
    try:
        delta = series.diff().dropna()
        gain = delta.clip(lower=0).rolling(period).mean().iloc[-1]
        loss = (-delta.clip(upper=0)).rolling(period).mean().iloc[-1]
        if loss == 0:
            return 100.0
        rs = gain / loss
        return round(100 - 100 / (1 + rs), 1)
    except Exception:
        return 50.0


def scan_opportunities(max_tickers: int | None = None) -> dict:
    """iter50/53: scanner READ-ONLY de oportunidades del mercado amplio.

    Escanea el S&P 500 COMPLETO + ETFs sectoriales/temáticos (+ benchmark)
    buscando momentum/tendencias emergentes y los clasifica por tipo de setup.
    SEPARADO de scan_for_candidates() — NO alimenta la rotación del universo ni
    el trading. Es puro research: surface oportunidades para que el usuario las
    VEA y decida. Escribe opportunities.json.

    iter53: removido el cap de 160 (que cortaba ~A–G alfabético y dejaba ciego
    al radar a NVDA/TSLA/V/WMT etc.) → escanea los ~503 nombres del S&P 500.
    `max_tickers=None` escanea todo; un entero limita (para tests). La descarga
    se hace en chunks para no chocar con el timeout de yfinance.

    Factores: momentum multi-timeframe (1s/1m/3m), fuerza relativa vs SPY,
    tendencia (precio vs SMA50/SMA200), distancia al máximo 52s, RSI.
    """
    import pandas as pd  # noqa
    from alpha_agent.data import market_data as _md
    from alpha_agent.config import BENCHMARK_TICKER
    try:
        from alpha_agent.config import SECTOR_MAP
    except Exception:
        SECTOR_MAP = {}
    try:
        from alpha_agent.config import get_effective_cp_universe
        in_universe = set(get_effective_cp_universe())
    except Exception:
        in_universe = set()

    sp500 = _get_sp500_tickers()
    if max_tickers:
        sp500 = sp500[:max_tickers]
    # Universo del radar: benchmark + ETFs (rotación de temas) + S&P 500 completo,
    # deduplicado preservando orden.
    seen: set[str] = set()
    universe: list[str] = []
    for t in [BENCHMARK_TICKER, *_RADAR_ETFS, *sp500]:
        if t and t not in seen:
            seen.add(t)
            universe.append(t)

    # Descarga en chunks de 100 — evita el timeout de 45s del download único de
    # yfinance con ~520 tickers (cada chunk queda holgado bajo el límite). Cada
    # chunk usa su propio cache (label distinto), estable día a día.
    frames = []
    _chunk = 100
    for _i in range(0, len(universe), _chunk):
        part = universe[_i:_i + _chunk]
        try:
            df_part = _md._download_close(part, f"opportunities_{_i // _chunk}")
            if df_part is not None and not df_part.empty:
                frames.append(df_part)
        except Exception as e:
            logger.warning("scan_opportunities: chunk %d falló %s", _i // _chunk, e)
    if not frames:
        logger.warning("scan_opportunities: sin datos de ningún chunk")
        return {"opportunities": [], "sectors": {}, "generated_at": datetime.now(timezone.utc).isoformat()}
    closes = pd.concat(frames, axis=1)
    closes = closes.loc[:, ~closes.columns.duplicated()]  # dedup columnas entre chunks

    def _pct(s, n):
        return float((s.iloc[-1] / s.iloc[-1 - n] - 1) * 100) if len(s) > n else 0.0

    spy = closes[BENCHMARK_TICKER].dropna() if BENCHMARK_TICKER in closes.columns else None
    spy_1m = _pct(spy, 21) if spy is not None else 0.0

    opps = []
    for t in closes.columns:
        if t == BENCHMARK_TICKER:
            continue
        s = closes[t].dropna()
        if len(s) < 60:
            continue
        price = float(s.iloc[-1])
        ret_1w, ret_1m, ret_3m = _pct(s, 5), _pct(s, 21), _pct(s, 63)
        sma50 = float(s.tail(50).mean())
        sma200 = float(s.tail(200).mean()) if len(s) >= 200 else float(s.mean())
        uptrend = price > sma50 > sma200
        hi_52w = float(s.tail(252).max())
        dist_hi = (price / hi_52w - 1) * 100 if hi_52w > 0 else -100
        rel_strength = ret_1m - spy_1m   # vs SPY
        rsi = _rsi(s)

        # Clasificación de setup (solo surface oportunidades reales)
        if dist_hi > -3 and ret_1m > 0:
            setup = "🚀 breakout"
        elif uptrend and ret_1m > 5:
            setup = "📈 momentum"
        elif rsi < 35 and ret_1w > 1:
            setup = "🔄 rebote"
        elif uptrend and rel_strength > 0:
            setup = "📊 tendencia"
        else:
            continue  # no es oportunidad

        # iter51: ETAPA del trend — "llegar temprano" vs perseguir el techo.
        dist_sma50 = (price / sma50 - 1) * 100 if sma50 > 0 else 0
        if rsi >= 75 or ret_1m >= 40 or dist_sma50 > 25:
            etapa = "🔴 tardía"        # parabólico — NO perseguir, riesgo reversión
        elif uptrend and dist_sma50 < 8 and 45 <= rsi <= 68:
            etapa = "🟢 temprana"      # trend joven — lo ideal
        else:
            etapa = "🟡 madura"        # en curso
        etapa_adj = {"🟢 temprana": 12, "🟡 madura": 0, "🔴 tardía": -15}[etapa]
        score = round(
            0.35 * ret_1m + 0.25 * ret_3m + 0.15 * ret_1w + 0.15 * rel_strength
            + (8 if uptrend else 0) + (6 if dist_hi > -5 else 0) + etapa_adj, 1
        )
        opps.append({
            "ticker": t,
            "score": score,
            "setup": setup,
            "etapa": etapa,
            "ret_1w": round(ret_1w, 1),
            "ret_1m": round(ret_1m, 1),
            "ret_3m": round(ret_3m, 1),
            "rel_strength": round(rel_strength, 1),
            "dist_52w_high": round(dist_hi, 1),
            "rsi": rsi,
            "sector": "ETF" if t in _RADAR_ETFS else SECTOR_MAP.get(t, "Other"),
            "is_etf": t in _RADAR_ETFS,
            "in_universe": t in in_universe,
        })

    opps.sort(key=lambda x: -x["score"])

    # Agregación sectorial: fuerza promedio por sector
    from collections import defaultdict
    sec_scores = defaultdict(list)
    for o in opps:
        sec_scores[o["sector"]].append(o["ret_1m"])
    sectors = {
        sec: {"avg_mom_1m": round(sum(v) / len(v), 1), "n": len(v)}
        for sec, v in sec_scores.items()
    }
    sectors = dict(sorted(sectors.items(), key=lambda kv: -kv[1]["avg_mom_1m"]))

    result = {
        "opportunities": opps[:25],
        "fresh": [o for o in opps[:25] if not o["in_universe"] and not o.get("is_etf")][:10],  # nombres nuevos (no ETFs)
        "sectors": sectors,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scanned": len(closes.columns),
    }
    try:
        OPPORTUNITIES_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug("no se pudo escribir opportunities.json: %s", e)
    logger.info("scan_opportunities: %d oportunidades de %d escaneados", len(opps), len(closes.columns))
    return result


def format_opportunities_digest(result: dict, top: int = 8) -> str:
    """Digest read-only de oportunidades para Telegram/WhatsApp."""
    opps = result.get("opportunities", [])
    if not opps:
        return "📡 Sin oportunidades destacadas hoy."
    sectors = result.get("sectors", {})
    lines = ["📡 *RADAR DE OPORTUNIDADES* (research, no auto-opera)"]
    top_sec = list(sectors.items())[:3]
    if top_sec:
        lines.append("Sectores fuertes: " + " · ".join(
            f"{s} {d['avg_mom_1m']:+.0f}%" for s, d in top_sec))
    lines.append("")
    for o in opps[:top]:
        tag = "" if o["in_universe"] else " 🆕"
        et = o.get("etapa", "")
        lines.append(f"{et} *{o['ticker']}*{tag} — 1m {o['ret_1m']:+.0f}% · vs SPY {o['rel_strength']:+.0f}% · {o.get('setup','')}")
    fresh = result.get("fresh", [])
    if fresh:
        lines.append(f"\n🆕 Fuera del universo: {', '.join(o['ticker'] for o in fresh[:6])}")
    lines.append("\n_Read-only. El sistema NO compra esto solo — vos decidís._")
    return "\n".join(lines)
