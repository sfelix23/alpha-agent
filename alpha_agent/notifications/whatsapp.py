"""
Notificaciones WhatsApp vía Twilio sandbox.

Twilio sandbox tiene dos limitaciones que nos pegaban:
    1. Límite duro de 1600 caracteres por mensaje.
    2. La sesión del sandbox expira cada 72h y hay que reenviar "join <palabra>".

Esta versión parte reportes largos en varios mensajes (≤ 1400 chars cada uno)
para evitar truncación, y loguea el status real que devuelve Twilio (queued /
sent / delivered / failed) así podés ver si el mensaje salió pero no llegó.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from alpha_agent.config import WHATSAPP_FROM

logger = logging.getLogger(__name__)

MAX_CHARS = 1400  # margen sobre el límite de 1600

_CIRCUIT_PATH = Path("signals/whatsapp_circuit.json")
_CIRCUIT_MAX_FAILURES = 3   # fallos consecutivos antes de abrir el circuito
_CIRCUIT_RESET_HOURS  = 6   # horas hasta reintentar después de abrir


def _circuit_open() -> bool:
    """True si el circuito está abierto (demasiados fallos recientes)."""
    try:
        if not _CIRCUIT_PATH.exists():
            return False
        data = json.loads(_CIRCUIT_PATH.read_text(encoding="utf-8"))
        if not data.get("tripped"):
            return False
        tripped_at = datetime.fromisoformat(data["tripped_at"])
        if datetime.now() - tripped_at > timedelta(hours=_CIRCUIT_RESET_HOURS):
            # Auto-reset después del período de espera
            _circuit_record(success=True)
            logger.info("WhatsApp circuit breaker auto-reset (%.0fh transcurridas)", _CIRCUIT_RESET_HOURS)
            return False
        return True
    except Exception:
        return False


def _circuit_record(success: bool) -> None:
    """Actualiza el estado del circuito; escrita atómica."""
    try:
        _CIRCUIT_PATH.parent.mkdir(exist_ok=True)
        current: dict = {}
        if _CIRCUIT_PATH.exists():
            try:
                current = json.loads(_CIRCUIT_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        if success:
            current = {"consecutive_failures": 0, "tripped": False}
        else:
            fails = current.get("consecutive_failures", 0) + 1
            tripped = fails >= _CIRCUIT_MAX_FAILURES
            current = {
                "consecutive_failures": fails,
                "tripped": tripped,
                "tripped_at": current.get("tripped_at") if current.get("tripped") else datetime.now().isoformat(),
            }
            if tripped and fails == _CIRCUIT_MAX_FAILURES:
                logger.error(
                    "WhatsApp circuit breaker ABIERTO tras %d fallos consecutivos — "
                    "sin intentos por %dh. Renovar sesión Twilio sandbox.",
                    fails, _CIRCUIT_RESET_HOURS,
                )
        tmp_fd, tmp_path = tempfile.mkstemp(dir=_CIRCUIT_PATH.parent, prefix=".tmp_wa_", suffix=".json")
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(current, f)
        os.replace(tmp_path, _CIRCUIT_PATH)
    except Exception as exc:
        logger.debug("Error actualizando WhatsApp circuit state: %s", exc)


def _chunk(body: str, max_chars: int = MAX_CHARS) -> List[str]:
    """
    Parte un mensaje en chunks que no cortan líneas a la mitad.
    Agrega header (k/N) si hay más de un chunk.
    """
    if len(body) <= max_chars:
        return [body]

    lines = body.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        # +1 por el \n
        if current_len + len(line) + 1 > max_chars and current:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line) + 1
        else:
            current.append(line)
            current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))

    n = len(chunks)
    return [f"[{i+1}/{n}]\n{c}" for i, c in enumerate(chunks)]


def send_whatsapp(mensaje: str, *, header: str = "REPORTE QUANT ALPHA") -> bool:
    """
    Envía un mensaje (potencialmente multi-parte) a `MY_PHONE_NUMBER` vía Twilio.
    Devuelve True si TODOS los chunks salieron sin excepción, False si alguno falló.
    """
    if _circuit_open():
        logger.warning(
            "WhatsApp circuit breaker abierto — skip envío. "
            "Renovar sesión Twilio sandbox y esperar auto-reset (%.0fh).",
            _CIRCUIT_RESET_HOURS,
        )
        return False

    sid = os.getenv("TWILIO_SID")
    token = os.getenv("TWILIO_TOKEN")
    to = os.getenv("MY_PHONE_NUMBER")
    if not (sid and token and to):
        logger.warning("Faltan credenciales Twilio o MY_PHONE_NUMBER en .env")
        return False

    try:
        from twilio.rest import Client  # type: ignore
    except ImportError:
        logger.error("Falta twilio: `pip install twilio`")
        return False

    client = Client(sid, token)

    full = f"📊 *{header}*\n\n{mensaje}"
    chunks = _chunk(full)
    logger.info("WhatsApp: partido en %d chunk(s)", len(chunks))

    ok = True
    for i, body in enumerate(chunks, 1):
        try:
            msg = client.messages.create(
                from_=WHATSAPP_FROM,
                body=body,
                to=f"whatsapp:{to}",
            )
            logger.info(
                "WhatsApp chunk %d/%d → sid=%s status=%s",
                i, len(chunks), msg.sid, msg.status,
            )
            # Si Twilio ya sabe que está undelivered/failed, advertir
            if msg.status in {"failed", "undelivered"}:
                logger.error(
                    "Twilio reportó %s para chunk %d — ¿sesión sandbox expirada? "
                    "Reenviá 'join <palabra>' al +14155238886",
                    msg.status, i,
                )
                ok = False
        except Exception as e:
            err_str = str(e).lower()
            logger.error("Error enviando chunk %d: %s", i, e)
            # Detectar errores típicos de sesión sandbox expirada y loguear instrucción específica
            if any(k in err_str for k in ("63016", "sandbox", "not opted", "not in session", "permission")):
                logger.error(
                    "🔒 Sesión Twilio sandbox expirada — enviá 'join <palabra>' "
                    "al +14155238886 desde el número %s para renovarla (cada 72h)",
                    to,
                )
            ok = False

    _circuit_record(success=ok)
    return ok
