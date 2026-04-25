"""
Health check — detecta si el bot no corrió y manda alerta.

Corre via Task Scheduler a las 12:30 ART (después del run de las 10:35).
Si signals/last_run.json tiene más de 4 horas → el bot falló → WhatsApp de alerta.

Uso:
    python run_health_check.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR   = Path(__file__).parent.resolve()
HEALTH_FILE = BASE_DIR / "signals" / "last_run.json"
LOG_DIR    = BASE_DIR / "logs"

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("health_check")


def main() -> None:
    load_dotenv(BASE_DIR / ".env")

    from alpha_agent.notifications import send_whatsapp

    now = datetime.now()

    # No alertar en fines de semana
    if now.weekday() >= 5:
        logger.info("Fin de semana — health check omitido.")
        return

    if not HEALTH_FILE.exists():
        msg = (
            f"⚠️ *HEALTH ALERT*\n"
            f"No se encontró signals/last_run.json\n"
            f"El bot nunca corrió o los archivos fueron borrados.\n"
            f"Hora: {now.strftime('%H:%M')}"
        )
        logger.warning("last_run.json no encontrado")
        send_whatsapp(msg)
        return

    try:
        data = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        last_run_str = data.get("last_run", "")
        status = data.get("status", "unknown")
        last_run = datetime.fromisoformat(last_run_str)
    except Exception as e:
        logger.error("Error leyendo health file: %s", e)
        send_whatsapp(f"⚠️ *HEALTH ALERT* — Error leyendo last_run.json: {e}")
        return

    age_hours = (now - last_run).total_seconds() / 3600
    logger.info("Último run: %s (hace %.1fh) — status: %s", last_run_str, age_hours, status)

    # Umbral: si el bot corrió pero falló
    if status in ("analyst_failed", "trader_failed"):
        send_whatsapp(
            f"⚠️ *HEALTH ALERT*\n"
            f"El bot corrió pero falló: `{status}`\n"
            f"Último intento: {last_run.strftime('%H:%M')}\n"
            f"Revisar logs: `logs/autonomous_{now.strftime('%Y-%m-%d')}.log`"
        )
        return

    # Umbral: si no corrió en las últimas 4 horas en día hábil
    if age_hours > 4.0:
        equity_str = data.get("equity", "N/D")
        send_whatsapp(
            f"⚠️ *HEALTH ALERT*\n"
            f"El bot no corrió en {age_hours:.1f}h\n"
            f"Último run exitoso: {last_run.strftime('%d/%m %H:%M')}\n"
            f"Equity registrado: ${equity_str}\n\n"
            f"Causas posibles:\n"
            f"• PC apagada (no Sleep)\n"
            f"• Error en Task Scheduler\n"
            f"• Python crash sin retry\n\n"
            f"Revisar: Task Scheduler → Alpha Analyst"
        )
        logger.warning("HEALTH ALERT enviado — bot no corrió en %.1fh", age_hours)
    else:
        logger.info("Sistema OK — último run hace %.1fh con status '%s'", age_hours, status)


if __name__ == "__main__":
    main()
