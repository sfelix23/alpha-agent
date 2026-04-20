from dotenv import load_dotenv
load_dotenv()
from pyngrok import ngrok, conf
import os

token  = os.getenv("NGROK_AUTHTOKEN")
domain = os.getenv("NGROK_DOMAIN")

print("Token:  ", token[:8]+"..." if token else "MISSING")
print("Domain: ", domain or "MISSING")

if not token or not domain:
    print("\nERROR: Falta token o dominio en .env")
    exit(1)

print("\nConectando ngrok...")
conf.get_default().auth_token = token
tunnel = ngrok.connect(5050, domain=domain)
print("URL publica:", tunnel.public_url)
print("\nOK - ngrok funciona. Cerrando tunel de prueba.")
ngrok.kill()
