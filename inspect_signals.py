"""
Utilitario: imprime un resumen compacto de signals/latest.json.
Lo invoca verify_and_trade.ps1 después del analyst.
"""

from __future__ import annotations

import json
import pathlib
import sys


def main() -> None:
    path = pathlib.Path("signals/latest.json")
    if not path.exists():
        print("  ✗ signals/latest.json no existe — corré primero run_analyst.py")
        sys.exit(1)

    data = json.loads(path.read_text(encoding="utf-8"))

    print(f"  generated_at : {data.get('generated_at', '?')}")
    print(f"  capital      : ${data.get('capital_usd', 0):.0f}")

    macro = data.get("macro", {})
    print(f"  régimen      : {macro.get('regime', '?')} — {macro.get('regime_reason', '')}")
    prices = macro.get("prices", {})
    if prices:
        vix = prices.get("vix")
        oil = prices.get("oil_wti")
        dxy = prices.get("dxy")
        parts = []
        if vix is not None:
            parts.append(f"VIX {vix:.1f}")
        if oil is not None:
            parts.append(f"WTI ${oil:.1f}")
        if dxy is not None:
            parts.append(f"DXY {dxy:.1f}")
        if parts:
            print(f"  macro        : {' · '.join(parts)}")

    lp = data.get("long_term", [])
    cp = data.get("short_term", [])
    opts = data.get("options_book", [])
    hedge = data.get("hedge_book", [])

    print(f"  LP equity    : {len(lp)} posiciones → {[s['ticker'] for s in lp]}")
    print(f"  CP equity    : {len(cp)} posiciones → {[s['ticker'] for s in cp]}")

    if opts:
        desc = []
        for s in opts:
            o = s.get("option", {}) or {}
            desc.append(f"{s['ticker']}-{o.get('type','?').upper()}@{o.get('strike','?')}→${o.get('contract_cost_est',0):.0f}")
        print(f"  opciones dir : {len(opts)} → {desc}")
    else:
        print(f"  opciones dir : 0")

    if hedge:
        desc = []
        for s in hedge:
            o = s.get("option", {}) or {}
            desc.append(f"{s['ticker']}-PUT@{o.get('strike','?')}→${o.get('contract_cost_est',0):.0f}")
        print(f"  hedge book   : {len(hedge)} → {desc}")
    else:
        print(f"  hedge book   : 0 (no necesario — régimen no bear / VIX bajo)")

    # Riesgo total si el stop pegara en todas las posiciones equity
    total_max_loss = 0.0
    for s in lp + cp:
        risk = s.get("thesis", {}).get("risk", {})
        total_max_loss += float(risk.get("max_loss_usd_if_stop_hit", 0) or 0)
    # Más las primas de todas las opciones (riesgo máximo de long options = prima total)
    for s in opts + hedge:
        o = s.get("option", {}) or {}
        total_max_loss += float(o.get("contract_cost_est", 0) or 0)
    cap = float(data.get("capital_usd", 1))
    pct = total_max_loss / cap * 100 if cap > 0 else 0
    print(f"  riesgo máx   : ${total_max_loss:.0f} ({pct:.1f}% del capital) si todos los stops pegan + primas se evaporan")


if __name__ == "__main__":
    main()
