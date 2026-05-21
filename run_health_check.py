"""
Health check — detecta si el ANALYST de Cloud Run no corrió y manda alerta.

iter22: reescrito. Antes leía signals/last_run.json LOCAL, que dejó de actualizarse
cuando Cloud Run reemplazó la ejecución local (~12/05) → falsas alarmas eternas
("bot no corrió en 217h"). Ahora mira la frescura REAL: el generated_at del
signals/latest.json que Cloud Run pushea al repo en cada daily. Umbral 26h
(el daily corre 1×/día hábil; 26h = se saltó un día). Fines de semana: omitido.

Corre via Task Scheduler local. Uso: python run_health_check.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.resolve()
RAW_URL = "https://raw.githubusercontent.com/sfelix23/alpha-agent/master/signals/latest.json"
STALE_HOURS = 26.0   # daily 1×/día hábil; >26h = se saltó (no el 4h del path local viejo)

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("health_check")


def _fetch_generated_at() -> datetime | None:
    """generated_at del latest.json en el repo (lo que Cloud Run actualiza)."""
    try:
        import requests
        r = requests.get(RAW_URL, timeout=15, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        g = (r.json() or {}).get("generated_at", "")
        if not g:
            return None
        dt = datetime.fromisoformat(g.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception as e:
        logger.warning("No pude leer latest.json del repo: %s", e)
        return None


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    from alpha_agent.notifications import send_whatsapp

    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        logger.info("Fin de semana — health check omitido.")
        return

    gen = _fetch_generated_at()
    if gen is None:
        logger.info("No se pudo determinar frescura del repo — no se alerta (evita falso positivo).")
        return

    age_hours = (now - gen).total_seconds() / 3600
    logger.info("latest.json (repo) generado hace %.1fh", age_hours)

    if age_hours > STALE_HOURS:
        send_whatsapp(
            f"⚠️ *HEALTH ALERT*\n"
            f"El analyst de Cloud Run no actualizó signals en {age_hours:.0f}h "
            f"(umbral {STALE_HOURS:.0f}h)\n"
            f"Último: {gen.strftime('%d/%m %H:%M')} UTC\n\n"
            f"Trigger manual:\n"
            f"`gcloud run jobs execute alpha-daily --region us-central1 --project alpha-agent-2025`"
        )
        logger.warning("HEALTH ALERT enviado — analyst Cloud Run stale %.1fh", age_hours)
    else:
        logger.info("Sistema OK — analyst Cloud Run corrió hace %.1fh.", age_hours)


if __name__ == "__main__":
    main()
