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

import logging
import os
from typing import List

from alpha_agent.config import WHATSAPP_FROM

logger = logging.getLogger(__name__)

MAX_CHARS = 1400  # margen sobre el límite de 1600


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
            logger.error("Error enviando chunk %d: %s", i, e)
            ok = False

    return ok
