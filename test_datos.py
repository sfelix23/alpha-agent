import os
import yfinance as yf
import pandas as pd
import numpy as np
import pandas_ta as ta
import google.generativeai as genai
from dotenv import load_dotenv
from twilio.rest import Client
from datetime import datetime

# --- 1. CONFIGURACIÓN Y ARRANQUE ---
load_dotenv()
print(f"[{datetime.now().strftime('%H:%M:%S')}] 🏛️ INICIANDO AGENTE ALPHA v5.8: UCEMA FULL ESTRATEGY")

# Configuración de IA con el modelo más compatible
try:
    genai.configure(api_key=os.getenv('GOOGLE_API_KEY'))
    # 'gemini-pro' es el nombre universal que evita el error 404 en la mayoría de las versiones
    model = genai.GenerativeModel('models/gemini-1.5-flash')
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ IA Conectada y Operativa.")
except Exception as e:
    print(f"❌ Error crítico en IA: {e}"); exit()

# --- 2. EL UNIVERSO COMPLETO (45 ACTIVOS) ---
ACTIVOS = {
    # 🛢️ ENERGÍA Y PETRÓLEO
    "Exxon": "XOM", "Chevron": "CVX", "Petrobras": "PBR", "Schlumberger": "SLB", 
    "Shell": "SHEL", "TotalEnergies": "TTE", "Vista": "VIST", "YPF": "YPF", "Pampa": "PAM",
    # ⚔️ DEFENSA Y GEOPOLÍTICA
    "Lockheed": "LMT", "Raytheon": "RTX", "Northrop": "NOC", "General_Dynamics": "GD", 
    "Boeing": "BA", "Palantir": "PLTR", "Anduril_Proxy": "AVAV",
    # 🇦🇷 ARGENTINA (Selección Estratégica)
    "Galicia": "GGAL", "Macro": "BMA", "TGS": "TGS", "Edenor": "EDN", "Pampa_Ar": "PAM",
    "MercadoLibre": "MELI", "Despegar": "DESP", "IRSA": "IRS", "Aluar": "ALUA.BA",
    # ⛏️ MINERÍA Y RIGI (Litio, Cobre, Oro)
    "Arcadium_Lithium": "ALTM", "Lithium_Americas": "LAC", "SQM": "SQM", 
    "Rio_Tinto": "RIO", "Vale": "VALE", "Barrick_Gold": "GOLD", "Newmont": "NEM", "Freeport_Cobre": "FCX",
    # 💻 TECNOLOGÍA E IA
    "Nvidia": "NVDA", "AMD": "AMD", "Microsoft": "MSFT", "Google": "GOOGL", 
    "Apple": "AAPL", "Meta": "META", "Tesla": "TSLA", "TSM_Taiwan": "TSM", "ASML": "ASML",
    # 🌊 BENCHMARKS Y ASSETS DE REFUGIO
    "Bitcoin_ETF": "IBIT", "Ethereum_ETF": "ETHE", "Nasdaq_100": "QQQ", "S&P500": "SPY", "Gold_ETF": "GLD"
}

def enviar_whatsapp(mensaje):
    """Lógica de notificación vía Twilio."""
    try:
        client = Client(os.getenv('TWILIO_SID'), os.getenv('TWILIO_TOKEN'))
        client.messages.create(
            from_='whatsapp:+14155238886', # Sandbox
            body=f"📊 *REPORTE QUANT ALPHA UCEMA*\n\n{mensaje}",
            to=f"whatsapp:{os.getenv('MY_PHONE_NUMBER')}"
        )
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 📱 WhatsApp enviado con éxito.")
    except Exception as e:
        print(f"❌ Error al enviar WhatsApp: {e}")

def analizar_activo(ticker, spy_returns):
    """Cálculos Financieros: CAPM (Beta), Markowitz (Retorno/Riesgo) y ATR."""
    try:
        df = yf.download(ticker, period="1y", interval="1d", progress=False)
        # ESCUDO: Si Yahoo no devuelve datos, salteamos para no romper el programa
        if df is None or df.empty or len(df) < 60: return None
        
        # 1. Alineación para CAPM (Beta)
        df['Ret'] = np.log(df['Close'] / df['Close'].shift(1))
        asset_ret_all = df['Ret'].dropna()
        common_dates = asset_ret_all.index.intersection(spy_returns.index)
        
        asset_ret = asset_ret_all.loc[common_dates].values.flatten()
        mkt_ret = spy_returns.loc[common_dates].values.flatten()
        
        # 2. Estadística Financiera
        beta = np.cov(asset_ret, mkt_ret)[0][1] / np.var(mkt_ret)
        mu_anual = asset_ret.mean() * 252 # Retorno Esperado
        sigma_anual = asset_ret.std() * np.sqrt(252) # Volatilidad (Riesgo)
        
        # 3. Indicadores Técnicos
        df['RSI'] = ta.rsi(df['Close'], length=14)
        df['ATR'] = ta.atr(df['High'], df['Low'], df['Close'], length=14)
        
        ultimo = df.tail(1).iloc[0]
        return {
            "ticker": ticker,
            "precio": round(float(ultimo['Close']), 2),
            "retorno_esp": f"{round(mu_anual*100, 2)}%",
            "volatilidad": f"{round(sigma_anual*100, 2)}%",
            "beta": round(float(beta), 2),
            "rsi": round(float(ultimo['RSI']), 2),
            "atr": round(float(ultimo['ATR']), 2)
        }
    except: return None

def ejecutar_mision():
    # Benchmark S&P 500 para el CAPM
    spy = yf.download("SPY", period="1y", interval="1d", progress=False)['Close']
    spy_ret = np.log(spy / spy.shift(1)).dropna()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🕵️ Escaneando el universo de 45 activos...")
    final_data = {}
    
    for nombre, ticker in ACTIVOS.items():
        res = analizar_activo(ticker, spy_ret)
        if res:
            final_data[nombre] = res
            print(f" -> {nombre} ({ticker}) analizado correctamente.")

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🧠 IA Generando Reporte Estratégico...")
    
    prompt = f"""
    Actúa como un Chief Investment Officer Senior. Analiza este dataset: {final_data}
    
    REQUERIMIENTOS DEL REPORTE:
    1. Define el TOP 5 de inversión basado en el Sharpe Ratio (Retorno/Volatilidad) y Beta razonable (< 1.5).
    2. Explica brevemente cada elección usando CAPM y el RSI (indica si está en sobreventa o tendencia).
    3. Para un capital de $100, ¿cuánto asignarías a cada una?
    4. Define un STOP LOSS TÉCNICO para cada una usando: 'Precio - (2 * ATR)'.
    5. Concluye con el horizonte de inversión (Corto/Medio plazo).
    
    Usa un lenguaje ejecutivo, profesional y directo para ser leído en WhatsApp.
    """
    
    try:
        reporte = model.generate_content(prompt).text
        return reporte
    except Exception as e:
        return f"Error en generación de IA: {e}"

if __name__ == "__main__":
    resultado = ejecutar_mision()
    print("\n" + "═"*70 + "\n" + resultado + "\n" + "═"*70)
    
    envio = input("\n¿Querés enviar este reporte a tu WhatsApp? (s/n): ")
    if envio.lower() == 's':
        enviar_whatsapp(resultado)