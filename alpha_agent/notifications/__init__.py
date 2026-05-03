"""Canales de notificación."""
from .whatsapp import send_whatsapp
from .telegram import send_telegram


def send_notification(text: str, *, header: str = "REPORTE QUANT ALPHA") -> None:
    """Envía a WhatsApp y Telegram simultáneamente (falla silenciosa en cada canal)."""
    send_whatsapp(text, header=header)
    send_telegram(text)


__all__ = ["send_whatsapp", "send_telegram", "send_notification"]
