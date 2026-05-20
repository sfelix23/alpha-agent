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
    """Descarga la lista del S&P 500 desde Wikipedia via pandas."""
    try:
        import pandas as pd
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = pd.read_html(url)
        df = tables[0]
        tickers = df["Symbol"].str.replace(".", "-", regex=False).tolist()
        logger.info("S&P 500: %d tickers descargados", len(tickers))
        return tickers
    except Exception as e:
        logger.warning("No se pudo descargar S&P 500: %s", e)
        # Fallback: lista reducida de large-caps no cubiertas
        return [
            "BRK-B", "JNJ", "UNH", "PG", "KO", "PEP", "WMT", "HD", "DIS",
            "NFLX", "ADBE", "CRM", "INTC", "QCOM", "MU", "TXN", "AVGO",
            "GE", "CAT", "DE", "HON", "MMM", "UPS", "FDX",
            "GS", "MS", "BAC", "WFC", "C", "AXP",
            "PFE", "MRK", "ABBV", "BMY", "GILD",
            "NEE", "D", "SO", "DUK",
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
