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
    Calcula Sharpe 3M y momentum 1M para un ticker.
    Devuelve None si no hay datos suficientes.
    """
    try:
        import yfinance as yf
        data = yf.download(ticker, period="3mo", progress=False, auto_adjust=True)
        if data is None or len(data) < 30:
            return None
        closes = data["Close"].dropna()
        if len(closes) < 20:
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
            "price": round(float(closes.iloc[-1]), 2),
        }
    except Exception:
        return None


def _claude_synthesize(candidates: list[dict], macro_context: dict) -> list[dict]:
    """
    Pasa los candidatos a Claude (Haiku) para síntesis y priorización.
    Claude decide cuáles merecen atención y por qué.
    """
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

        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()

        # Extraer JSON
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text)
        return result.get("top_picks", [])

    except Exception as e:
        logger.warning("Claude synthesis falló: %s — usando ranking por Sharpe", e)
        return [
            {"ticker": c["ticker"], "prioridad": "MEDIA",
             "razon": f"Sharpe 3M: {c['sharpe_3m']:.2f}, momentum 1M: {c['mom_1m_pct']:+.1f}%",
             "riesgo": "Sin análisis Claude disponible"}
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
