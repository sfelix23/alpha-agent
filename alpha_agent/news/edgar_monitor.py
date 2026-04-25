"""
SEC EDGAR 8-K Monitor — detecta eventos materiales antes que el mercado.

Usa el RSS feed público de EDGAR para monitorear 8-K filings
de los tickers del universo. Cuando hay un filing nuevo, lo analiza
con Claude Sonnet para determinar si es material y cómo impacta.

Tipos de 8-K que importan:
  1.01 — Acuerdo material (M&A, contratos grandes)
  1.02 — Terminación de acuerdo
  2.02 — Resultados de operaciones (earnings)
  5.02 — Cambio en CEO/CFO (señal de alerta)
  8.01 — Otros eventos materiales
  2.04 — Trigger de aceleración de deuda
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_CACHE_PATH = Path(__file__).resolve().parents[2] / "signals" / "edgar_cache.json"
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "AlphaAgent/1.0 nfelix.geo@gmail.com",  # EDGAR requiere user-agent
    "Accept": "application/json",
})

_HIGH_IMPACT_ITEMS = {"1.01", "1.02", "2.02", "5.02", "8.01", "2.04", "4.01", "4.02"}


# ── Cache ────────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    if _CACHE_PATH.exists():
        try:
            return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"seen": [], "last_run": None}


def _save_cache(cache: dict) -> None:
    try:
        _CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug("edgar_cache save error: %s", e)


# ── EDGAR RSS ────────────────────────────────────────────────────────────────

def _get_cik(ticker: str) -> str | None:
    """Obtiene el CIK de EDGAR para un ticker."""
    try:
        url = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&forms=8-K"
        r = _SESSION.get(
            "https://data.sec.gov/submissions/",
            params={"q": ticker},
            timeout=8,
        )
        # Alternativa: usar el endpoint de company tickers
        r2 = _SESSION.get(
            "https://efts.sec.gov/LATEST/search-index?q=%22%22&forms=8-K&entity="
            + ticker,
            timeout=8,
        )
    except Exception:
        pass

    # Método más confiable: company_tickers.json cacheado por EDGAR
    try:
        r = _SESSION.get(
            "https://www.sec.gov/files/company_tickers.json",
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            for _, info in data.items():
                if info.get("ticker", "").upper() == ticker.upper():
                    return str(info["cik_str"]).zfill(10)
    except Exception as e:
        logger.debug("CIK lookup failed for %s: %s", ticker, e)
    return None


def _get_recent_8k(cik: str, days: int = 3) -> list[dict]:
    """Descarga los 8-K recientes de una empresa via EDGAR API."""
    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        r = _SESSION.get(url, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        filings = data.get("filings", {}).get("recent", {})

        forms      = filings.get("form", [])
        dates      = filings.get("filingDate", [])
        accessions = filings.get("accessionNumber", [])
        descriptions = filings.get("primaryDocument", [])

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        results = []
        for i, form in enumerate(forms):
            if form != "8-K":
                continue
            try:
                filing_date = datetime.fromisoformat(dates[i]).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if filing_date < cutoff:
                break  # EDGAR ordena por fecha desc, podemos parar
            acc = accessions[i].replace("-", "")
            results.append({
                "cik": cik,
                "accession": accessions[i],
                "date": dates[i],
                "primary_doc": descriptions[i] if i < len(descriptions) else "",
                "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/",
            })
        return results
    except Exception as e:
        logger.debug("8-K fetch failed for CIK %s: %s", cik, e)
        return []


def _fetch_filing_text(filing: dict) -> str:
    """Descarga el texto del 8-K (primeros 3000 chars para no exceder tokens)."""
    try:
        base_url = filing["url"]
        doc = filing.get("primary_doc", "")
        if not doc:
            return ""
        url = base_url + doc
        r = _SESSION.get(url, timeout=12)
        if r.status_code != 200:
            return ""
        text = r.text
        # Limpiar HTML básico
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:3500]
    except Exception as e:
        logger.debug("Filing text fetch error: %s", e)
        return ""


# ── Claude Sonnet Analysis ───────────────────────────────────────────────────

def _analyze_with_sonnet(ticker: str, filing_text: str, filing_date: str) -> dict | None:
    """
    Analiza un 8-K con Claude Sonnet para determinar impacto en precio.
    Usa Sonnet (no Haiku) porque los 8-K contienen lenguaje legal complejo.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        return None

    prompt = f"""Sos un analista de eventos especiales en un hedge fund.
Analizá este 8-K filing de {ticker} (fecha: {filing_date}) y determiná el impacto en precio.

TEXTO DEL FILING (extracto):
{filing_text[:2500]}

Respondé con JSON únicamente:
{{
  "material": true/false,
  "item_type": "tipo de evento (ej: earnings, CEO change, M&A, debt, other)",
  "sentiment": "BULLISH|BEARISH|NEUTRAL",
  "confidence": 0.0-1.0,
  "impact_pct": estimacion de movimiento de precio en % (positivo=sube, negativo=baja, 0=neutral),
  "summary": "1 línea: qué pasó exactamente",
  "action": "BUY_MORE|REDUCE|HOLD|MONITOR"
}}

Guías:
- material=true solo si el evento cambia el outlook de 3-12 meses
- earnings beat/miss → siempre material
- CEO change inesperado → material, generalmente bearish
- M&A anuncio → material, bullish para target
- Refinanciamiento de deuda → material si cambia estructura capital
- Notas legales rutinarias → material=false
- impact_pct: sé conservador (±2-8% para la mayoría, ±10-20% solo para eventos extremos)"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        result = json.loads(match.group())
        result["ticker"] = ticker
        result["filing_date"] = filing_date
        return result
    except Exception as e:
        logger.debug("Sonnet 8-K analysis failed: %s", e)
        return None


# ── Public API ───────────────────────────────────────────────────────────────

_CIK_CACHE: dict[str, str | None] = {}


def scan_edgar_filings(tickers: list[str], days: int = 2) -> list[dict]:
    """
    Escanea los 8-K de los últimos `days` días para los tickers dados.

    Returns lista de análisis con: ticker, material, sentiment, impact_pct, summary, action.
    Solo retorna filings materiales (filtra ruido).
    """
    cache = _load_cache()
    seen_accessions: set[str] = set(cache.get("seen", []))
    results: list[dict] = []
    new_seen: list[str] = []

    for ticker in tickers:
        if ticker.endswith(".BA"):
            continue  # EDGAR no cubre Buenos Aires

        # Obtener CIK (cacheado en memoria)
        if ticker not in _CIK_CACHE:
            _CIK_CACHE[ticker] = _get_cik(ticker)
            time.sleep(0.2)  # EDGAR rate limit: 10 req/s

        cik = _CIK_CACHE.get(ticker)
        if not cik:
            continue

        filings = _get_recent_8k(cik, days=days)
        time.sleep(0.2)

        for filing in filings:
            acc = filing["accession"]
            if acc in seen_accessions:
                continue

            new_seen.append(acc)
            text = _fetch_filing_text(filing)
            if not text or len(text) < 200:
                continue

            analysis = _analyze_with_sonnet(ticker, text, filing["date"])
            if analysis and analysis.get("material"):
                results.append(analysis)
                logger.info(
                    "EDGAR 8-K %s (%s): %s %s (impact %.1f%%)",
                    ticker, filing["date"],
                    analysis.get("sentiment"), analysis.get("item_type"),
                    analysis.get("impact_pct", 0),
                )

    # Actualizar cache (keep last 500 accessions)
    cache["seen"] = list(seen_accessions | set(new_seen))[-500:]
    cache["last_run"] = datetime.now().isoformat()
    _save_cache(cache)

    return results


def format_edgar_alerts(analyses: list[dict]) -> str:
    """Formatea los alertas de EDGAR para WhatsApp."""
    if not analyses:
        return ""
    lines = ["📋 *SEC 8-K EVENTOS MATERIALES*"]
    for a in analyses:
        emoji = "🟢" if a.get("sentiment") == "BULLISH" else "🔴" if a.get("sentiment") == "BEARISH" else "🟡"
        impact = a.get("impact_pct", 0)
        sign = "+" if impact >= 0 else ""
        lines.append(
            f"{emoji} *{a['ticker']}* [{a.get('item_type','?')}] "
            f"{sign}{impact:.1f}% estimado\n"
            f"   {a.get('summary', '')}\n"
            f"   → Acción: {a.get('action','MONITOR')}"
        )
    return "\n".join(lines)
