"""
Capital Tracker — resuelve el problema de cuenta paper con $100k vs $1600 real.

Alpaca paper da $100k por defecto. Este módulo registra el equity de Alpaca
en el primer run y calcula el equity "virtual" que refleja los $1600 reales:

    virtual_equity = OUR_START + (alpaca_current - alpaca_baseline)

Así los retornos % son idénticos a los de Alpaca (correctos), pero los
números absolutos reflejan la realidad de $1600 iniciales.

Archivo de estado: signals/capital_baseline.json
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_BASELINE_PATH = Path("signals/capital_baseline.json")
OUR_START = 1600.0   # capital real del usuario


def get_virtual_equity(alpaca_equity: float) -> float:
    """
    Calcula el equity virtual en base a los $1600 iniciales.

    En el primer call registra el equity de Alpaca como baseline.
    En calls posteriores computa: virtual = OUR_START + (alpaca - baseline).

    Args:
        alpaca_equity: equity actual de la cuenta Alpaca paper.

    Returns:
        float — equity virtual en dólares (base $1600).
    """
    _BASELINE_PATH.parent.mkdir(exist_ok=True)

    if _BASELINE_PATH.exists():
        try:
            data = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
            baseline = float(data["alpaca_baseline_equity"])
            our_start = float(data.get("our_starting_capital", OUR_START))
            virtual = our_start + (alpaca_equity - baseline)
            logger.info(
                "Virtual equity: $%.2f (Alpaca $%.2f, baseline $%.2f, start $%.2f)",
                virtual, alpaca_equity, baseline, our_start,
            )
            return max(virtual, 0.0)
        except Exception as exc:
            logger.warning("Error leyendo capital_baseline: %s — reinicializando", exc)

    # Primera vez: registrar baseline
    data = {
        "alpaca_baseline_equity": round(alpaca_equity, 2),
        "our_starting_capital": OUR_START,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "note": "No tocar manualmente. Reset via reset_capital_baseline().",
    }
    _BASELINE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info(
        "Capital baseline registrado: Alpaca $%.2f → virtual $%.2f (primer run)",
        alpaca_equity, OUR_START,
    )
    return OUR_START


def reset_capital_baseline(alpaca_equity: float, new_start: float = OUR_START) -> None:
    """
    Resetea el baseline — usar cuando se añade capital real o se resetea la cuenta.

    Ejemplo: el usuario deposita $200 más → new_start = 1800.
    """
    data = {
        "alpaca_baseline_equity": round(alpaca_equity, 2),
        "our_starting_capital": round(new_start, 2),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "note": f"Reset manual el {datetime.now().strftime('%Y-%m-%d')}",
    }
    _BASELINE_PATH.parent.mkdir(exist_ok=True)
    _BASELINE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Capital baseline reseteado: start=$%.2f sobre Alpaca $%.2f", new_start, alpaca_equity)


def get_initial_capital() -> float:
    """Devuelve el capital inicial registrado (nuestro $1600 o lo que haya)."""
    try:
        data = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
        return float(data.get("our_starting_capital", OUR_START))
    except Exception:
        return OUR_START
