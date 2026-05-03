# VPS Migration Guide

## Opciones recomendadas (orden de prioridad)

| Opción | Precio | CPU | RAM | Veredicto |
|--------|--------|-----|-----|-----------|
| **Hetzner CAX11** | €3.29/mes | 2 ARM | 4 GB | ⭐ Mejor relación precio/calidad |
| Oracle Cloud Free | $0 | 2-4 ARM | 12 GB | Gratis pero registro complicado |
| DigitalOcean Basic | $6/mes | 1 vCPU | 1 GB | Más caro, menos recursos |

---

## Opción A — Hetzner (recomendado)

### 1. Crear cuenta y servidor

1. Ir a [hetzner.com](https://www.hetzner.com/cloud) → Cloud → Add Server
2. Configuración:
   - **Location**: Ashburn (menor latencia a NYSE/NASDAQ)
   - **Image**: Ubuntu 24.04
   - **Type**: CAX11 (ARM64, 2 vCPU, 4GB, €3.29/mes)
   - **SSH Key**: agregar tu clave pública (o usar contraseña)
3. Crear → anotar la IP

### 2. Conectarse y hacer setup

```bash
# Desde tu PC local
ssh root@TU_IP_HETZNER

# Ejecutar el setup automático
curl -fsSL https://raw.githubusercontent.com/sfelix23/alpha-agent/master/vps/setup_vps.sh | bash
```

### 3. Copiar credenciales

```bash
# Desde tu PC local (en D:\Agente)
scp .env root@TU_IP_HETZNER:/opt/agente/.env
ssh root@TU_IP_HETZNER "chown agente:agente /opt/agente/.env && chmod 600 /opt/agente/.env"
```

### 4. Test rápido

```bash
ssh root@TU_IP_HETZNER
sudo -u agente bash -c 'cd /opt/agente && source .venv/bin/activate && python -c "from dotenv import load_dotenv; load_dotenv(); from trader_agent.brokers.alpaca_broker import AlpacaBroker; b=AlpacaBroker(paper=True); print(b.get_equity())"'
```

Si devuelve el equity de Alpaca → todo listo.

### 5. Verificar cron

```bash
sudo -u agente crontab -l   # ver las tareas
date                         # verificar timezone = ART
```

---

## Opción B — Oracle Cloud Always Free

1. Crear cuenta en [cloud.oracle.com](https://www.oracle.com/cloud/free/)
   - Requiere tarjeta de crédito para verificación (no cobra)
   - Elegir región más cercana: São Paulo o Ashburn
2. Compute → Instances → Create Instance
   - Shape: VM.Standard.A1.Flex (ARM) → 2 OCPU, 12GB RAM
   - Image: Ubuntu 22.04
3. Seguir los mismos pasos que Hetzner desde el punto 2

---

## Updates automáticos del código

El sistema se actualiza automáticamente con cada `git push`:

```bash
# En el VPS, agregar al cron (ya incluido en setup_vps.sh):
# Cada domingo a las 3 AM, hace git pull
0 3 * * 0  cd /opt/agente && git pull --ff-only
```

O manualmente:
```bash
ssh root@TU_IP && sudo -u agente bash -c 'cd /opt/agente && git pull'
```

---

## Monitorear el VPS

```bash
# Ver todos los logs de hoy
ssh root@TU_IP tail -f /opt/agente/logs/analyst_$(date +%Y-%m-%d).log

# Ver si cron está corriendo
ssh root@TU_IP systemctl status cron

# Ver uso de recursos
ssh root@TU_IP htop
```

---

## Diferencias vs Windows

| Windows | VPS Linux |
|---------|-----------|
| Task Scheduler | crontab |
| `run_autonomous.ps1` | cron directo → `run_analyst.py` |
| `D:\Agente\` | `/opt/agente/` |
| Sleep/Wake | Siempre encendido |
| Reinicio manual | Auto-restart con cron |

El código Python no necesita cambios — usa `pathlib` que es cross-platform.

---

## Costo total estimado

| Servicio | Costo |
|----------|-------|
| Hetzner CAX11 | €3.29/mes |
| Dominio (opcional) | ~$10/año |
| **Total** | **~€3.29/mes (~$3.60 USD)** |

Menos del 0.25% del capital administrado por mes.
