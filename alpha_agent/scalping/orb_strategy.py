"""
Opening Range Breakout (ORB) — estrategia de scalping.

Protocolo:
  9:30-9:45 EDT  → construir el rango de apertura (high/low de los primeros 15 min)
  9:45-14:00 EDT → vigilar breakout: precio cierra por encima del high (LONG)
                   o por debajo del low (SHORT) con volumen 1.3x promedio
  Entrada inmediata con bracket order
  SL = lado opuesto del rango (max 0.5% del precio)
  TP = 1.5× el tamaño del rango (mínimo 0.4%, máximo 1.5%)
  EOD close = 15:45 EDT

Notas:
  - Solo 1 trade activo por ticker
  - Máx 4 trades por día total
  - Requiere proceso continuo (NO es compatible con GitHub Actions)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Tickers de alta liquidez y spread apretado — universe del scalper
SCALP_UNIVERSE = [
    "SPY", "QQQ", "NVDA", "AMD", "TSLA", "AAPL", "META",
    "AMZN", "GOOGL", "MSFT", "COIN", "MELI", "PLTR",
]

# Parámetros del ORB
ORB_MINUTES   = 15       # duración del rango de apertura
MIN_RANGE_PCT = 0.002    # rango mínimo = 0.2% del precio (filtra días flatlines)
MAX_RANGE_PCT = 0.025    # rango máximo = 2.5% (demasiado volátil = skip)

# Parámetros de la posición
SCALP_BUDGET      = 400.0   # USD por trade
SCALP_SL_FLOOR    = 0.003   # stop mínimo 0.3%
SCALP_SL_CAP      = 0.005   # stop máximo 0.5%
SCALP_TP_MULT     = 1.5     # TP = 1.5 × tamaño del rango
SCALP_TP_MIN      = 0.004   # TP mínimo 0.4%
SCALP_TP_MAX      = 0.015   # TP máximo 1.5%
VOL_CONFIRM_MULT  = 1.3     # volumen del breakout debe ser ≥ 1.3× promedio 5 bars
MAX_DAILY_TRADES  = 4       # máx trades por día
EOD_CLOSE_HOUR_ET = 15      # cerrar todo a las 15:45 EDT
EOD_CLOSE_MIN_ET  = 45


@dataclass
class ORBState:
    ticker:     str
    orb_high:   float = 0.0
    orb_low:    float = 0.0
    orb_vol:    float = 0.0       # volumen promedio durante el rango
    range_pct:  float = 0.0       # (orb_high - orb_low) / orb_low
    locked:     bool  = False     # True cuando el rango está construido
    traded:     bool  = False     # True cuando ya se ejecutó un trade hoy
    bars:       list  = field(default_factory=list)   # bars 1-min del ORB

    def update(self, bar: dict) -> None:
        """Agrega un bar al rango (solo durante los primeros ORB_MINUTES)."""
        self.bars.append(bar)
        highs  = [b["h"] for b in self.bars]
        lows   = [b["l"] for b in self.bars]
        vols   = [b["v"] for b in self.bars]
        self.orb_high  = max(highs)
        self.orb_low   = min(lows)
        self.orb_vol   = sum(vols) / max(len(vols), 1)
        self.range_pct = (self.orb_high - self.orb_low) / max(self.orb_low, 0.01)

    def is_valid(self) -> bool:
        return MIN_RANGE_PCT <= self.range_pct <= MAX_RANGE_PCT

    def check_breakout(self, bar: dict) -> str | None:
        """
        Evalúa si el último bar es un breakout válido.
        Retorna "LONG", "SHORT" o None.
        """
        if self.traded or not self.locked or not self.is_valid():
            return None
        close   = bar["c"]
        vol     = bar["v"]
        vol_ok  = vol >= self.orb_vol * VOL_CONFIRM_MULT

        if close > self.orb_high and vol_ok:
            return "LONG"
        if close < self.orb_low and vol_ok:
            return "SHORT"
        return None


def compute_bracket(direction: str, entry: float, orb_state: ORBState) -> dict:
    """
    Calcula SL y TP para un breakout ORB.

    Para LONG:  SL = orb_low (o entry - SCALP_SL_CAP), TP = entry + 1.5×rango
    Para SHORT: SL = orb_high (o entry + SCALP_SL_CAP), TP = entry - 1.5×rango
    """
    rng = orb_state.orb_high - orb_state.orb_low

    if direction == "LONG":
        raw_sl = orb_state.orb_low
        sl_pct = (entry - raw_sl) / entry
        sl_pct = max(SCALP_SL_FLOOR, min(SCALP_SL_CAP, sl_pct))
        sl     = round(entry * (1 - sl_pct), 2)
        raw_tp = entry + rng * SCALP_TP_MULT
        tp_pct = (raw_tp - entry) / entry
        tp_pct = max(SCALP_TP_MIN, min(SCALP_TP_MAX, tp_pct))
        tp     = round(entry * (1 + tp_pct), 2)
        rr     = tp_pct / sl_pct
    else:
        raw_sl = orb_state.orb_high
        sl_pct = (raw_sl - entry) / entry
        sl_pct = max(SCALP_SL_FLOOR, min(SCALP_SL_CAP, sl_pct))
        sl     = round(entry * (1 + sl_pct), 2)
        raw_tp = entry - rng * SCALP_TP_MULT
        tp_pct = (entry - raw_tp) / entry
        tp_pct = max(SCALP_TP_MIN, min(SCALP_TP_MAX, tp_pct))
        tp     = round(entry * (1 - tp_pct), 2)
        rr     = tp_pct / sl_pct

    qty    = max(1, int(SCALP_BUDGET / entry))
    notional = qty * entry

    return {
        "direction": direction,
        "entry":     entry,
        "sl":        sl,
        "tp":        tp,
        "sl_pct":    round(sl_pct * 100, 2),
        "tp_pct":    round(tp_pct * 100, 2),
        "rr":        round(rr, 2),
        "qty":       qty,
        "notional":  round(notional, 2),
        "range_pct": round(orb_state.range_pct * 100, 3),
    }


def is_in_orb_window(now_et_hour: int, now_et_min: int) -> bool:
    """True durante la ventana de construcción del rango (9:30-9:45)."""
    if now_et_hour == 9 and 30 <= now_et_min < 30 + ORB_MINUTES:
        return True
    return False


def is_in_trading_window(now_et_hour: int, now_et_min: int) -> bool:
    """True durante la ventana de búsqueda de breakouts (9:45-15:45)."""
    if now_et_hour == 9 and now_et_min >= 45:
        return True
    if 10 <= now_et_hour < EOD_CLOSE_HOUR_ET:
        return True
    if now_et_hour == EOD_CLOSE_HOUR_ET and now_et_min < EOD_CLOSE_MIN_ET:
        return True
    return False


def is_eod(now_et_hour: int, now_et_min: int) -> bool:
    """True cuando hay que cerrar todas las posiciones (≥ 15:45)."""
    return now_et_hour > EOD_CLOSE_HOUR_ET or (
        now_et_hour == EOD_CLOSE_HOUR_ET and now_et_min >= EOD_CLOSE_MIN_ET
    )
