"""Actualiza DuckDNS con la IP publica actual. Corre al inicio y cada hora."""
import os, urllib.request, logging
from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger(__name__)

def update():
    token  = os.getenv("DUCKDNS_TOKEN")
    domain = os.getenv("DUCKDNS_DOMAIN")
    if not token or not domain:
        return
    url = f"https://www.duckdns.org/update?domains={domain}&token={token}&ip="
    try:
        resp = urllib.request.urlopen(url, timeout=10).read().decode()
        if resp.strip() == "OK":
            # get current IP for logging
            ip = urllib.request.urlopen("https://api.ipify.org", timeout=10).read().decode()
            log.info("DuckDNS actualizado: %s.duckdns.org → %s", domain, ip)
        else:
            log.warning("DuckDNS respuesta inesperada: %s", resp)
    except Exception as e:
        log.warning("DuckDNS update failed: %s", e)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    update()
