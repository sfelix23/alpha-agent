"""
Configuración central del alpha_agent.

Toda constante, parámetro o universo de activos se define acá.
Si tenés que cambiar la lista de activos, los pesos LP/CP, o la tasa libre
de riesgo, lo hacés solo en este archivo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# ────────────────────────────────────────────────────────────────────────────
# UNIVERSO DE ACTIVOS (49)
# ────────────────────────────────────────────────────────────────────────────
ACTIVOS: dict[str, str] = {
    # Energía y petróleo
    "Exxon": "XOM", "Chevron": "CVX", "Petrobras": "PBR", "Schlumberger": "SLB",
    "Shell": "SHEL", "TotalEnergies": "TTE", "Vista": "VIST", "YPF": "YPF", "Pampa": "PAM",
    # Defensa y geopolítica
    "Lockheed": "LMT", "Raytheon": "RTX", "Northrop": "NOC", "General_Dynamics": "GD",
    "Boeing": "BA", "Palantir": "PLTR", "Anduril_Proxy": "AVAV",
    # Argentina (NYSE ADRs) — alto beta, asimétrico con recuperación macro AR
    "Galicia": "GGAL", "Macro": "BMA", "TGS": "TGS", "Edenor": "EDN",
    "MercadoLibre": "MELI", "Despegar": "DESP", "IRSA": "IRS",
    "Loma_Negra": "LOMA", "Transportadora_Gas_Norte": "TGNO4.BA",
    # Growth / momentum — alto potencial de retorno asimétrico
    "Broadcom": "AVGO", "Netflix": "NFLX", "CrowdStrike": "CRWD", "Coinbase": "COIN",
    # Minería / Litio / Cobre / Oro / Uranio
    "Arcadium_Lithium": "ALTM", "Lithium_Americas": "LAC", "SQM": "SQM",
    "Rio_Tinto": "RIO", "Vale": "VALE", "Barrick_Gold": "GOLD", "Newmont": "NEM",
    "Freeport_Cobre": "FCX", "Cameco_Uranium": "CCJ",
    # Tecnología e IA
    "Nvidia": "NVDA", "AMD": "AMD", "Microsoft": "MSFT", "Google": "GOOGL",
    "Apple": "AAPL", "Meta": "META", "Tesla": "TSLA", "TSM_Taiwan": "TSM", "ASML": "ASML",
    "Amazon": "AMZN",
    # Healthcare / Consumer / Financials
    "Eli_Lilly": "LLY",
    "JPMorgan": "JPM",
    # Benchmarks y refugio
    "Bitcoin_ETF": "IBIT", "Ethereum_ETF": "ETHE", "Nasdaq_100": "QQQ", "S&P500": "SPY", "Gold_ETF": "GLD",
}

# Benchmark de mercado para CAPM
BENCHMARK_TICKER: str = "SPY"

# Lista de tickers que NO entran en optimización Markowitz
# (son benchmarks o ETFs que solo usamos como referencia)
EXCLUIR_DE_OPTIMIZACION: set[str] = {"SPY", "QQQ", "GLD", "IBIT", "ETHE"}

# Mapeo ticker → sector, para el guard de concentración sectorial.
# Si un ticker no está acá, se etiqueta como "Other".
SECTOR_MAP: dict[str, str] = {
    # Energía / Petróleo
    "XOM": "Energy", "CVX": "Energy", "PBR": "Energy", "SLB": "Energy",
    "SHEL": "Energy", "TTE": "Energy", "VIST": "Energy", "YPF": "Energy",
    "PAM": "Energy", "TGS": "Energy", "EDN": "Energy",
    # Defensa
    "LMT": "Defense", "RTX": "Defense", "NOC": "Defense", "GD": "Defense",
    "BA": "Defense", "PLTR": "Defense", "AVAV": "Defense",
    # Minería / materiales / uranio
    "ALTM": "Materials", "LAC": "Materials", "SQM": "Materials", "RIO": "Materials",
    "VALE": "Materials", "GOLD": "Materials", "NEM": "Materials", "FCX": "Materials",
    "ALUA.BA": "Materials", "CCJ": "Materials",
    # Tech / Semis / mega-cap
    "NVDA": "Tech", "AMD": "Tech", "MSFT": "Tech", "GOOGL": "Tech",
    "AAPL": "Tech", "META": "Tech", "TSLA": "Tech", "TSM": "Tech", "ASML": "Tech",
    "AMZN": "Tech",
    # Healthcare / Financials US
    "LLY": "Healthcare", "JPM": "Financials",
    # Financials Argentina + consumer
    "GGAL": "Financials", "BMA": "Financials",
    "MELI": "Consumer", "DESP": "Consumer", "IRS": "RealEstate",
    "LOMA": "Materials", "TGNO4.BA": "Energy",
    # Growth US
    "AVGO": "Tech", "NFLX": "Consumer", "CRWD": "Tech", "COIN": "Crypto",
    # ETFs / refugio
    "SPY": "ETF", "QQQ": "ETF", "GLD": "ETF", "IBIT": "Crypto", "ETHE": "Crypto",
}

# Máximo % del libro LP por sector (guard de diversificación)
MAX_SECTOR_WEIGHT_LP: float = 0.40      # ningún sector > 40% del sleeve LP
# Correlación máxima permitida entre dos activos LP (rolling 1y)
MAX_PAIR_CORRELATION: float = 0.80


# ────────────────────────────────────────────────────────────────────────────
# PARÁMETROS FINANCIEROS
# ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FinancialParams:
    """Parámetros macro y de modelado.

    Capital real objetivo del usuario: USD 1600. Usamos ese mismo número para
    paper trading de modo que los fills, slippage y comisiones reflejen la
    realidad futura cuando se pase a real money.
    """

    # Capital de referencia (paper trading == capital real planeado)
    paper_capital_usd: float = 1600.0

    risk_free_rate: float = 0.045         # tasa libre de riesgo anual (USD ~ 10y T-Note)
    trading_days: int = 252
    history_period: str = "2y"            # rango para descargar data histórica (2y: 252 warm-up + 252 backtest)
    history_interval: str = "1d"
    min_obs: int = 60                     # mínimo de observaciones para incluir un activo

    # ── CONCENTRATED MODE para $1,600 ──────────────────────────────────────────
    # Con capital chico, la diversificación destruye retornos.
    # Estrategia: 2 ideas LP de alta convicción + 1 CP momentum + cash reserva.
    # Lógica: si una idea 3x, impacta 37-50% del portfolio (no el 14% de antes).
    # Sleeves: 60% LP (2 pos) + 20% CP (1 pos) + 10% opciones call + 10% cash
    weight_long_term: float = 0.60
    weight_short_term: float = 0.20
    weight_options: float = 0.10          # calls sobre la idea #1

    # Filtros LP — más selectivos en concentrated mode
    max_beta_lp: float = 1.8             # aceptar beta más alto = más upside en bull
    min_sharpe_lp: float = 0.40

    # Parámetros técnicos para CP
    rsi_oversold: float = 35.0
    rsi_overbought: float = 70.0
    atr_stop_multiple: float = 2.0        # stop loss = precio - 2*ATR

    # Concentrated: 2 LP (alta convicción, ~$480/pos) + 1 CP (momentum, ~$320)
    # Con $1600: LP = $960 ÷ 2 = $480 cada una, CP = $320, OPT = $160
    top_n_long_term: int = 2
    top_n_short_term: int = 1

    # Bucket options — call sobre el pick #1 del LP
    top_n_bearish: int = 1                # máx 1 put direccional
    max_contracts_per_trade: int = 1      # 1 contrato por trade (capital limitado)

    # Concentración máxima — hasta 50% en la mejor idea
    max_weight_per_asset: float = 0.50    # 50% max en un solo nombre

    # Kill switch — concentrated mode acepta más volatilidad intradía
    # Con 2 posiciones concentradas, un -3% puede ser ruido normal en días volátiles
    max_daily_drawdown: float = 0.05      # 5% ($80 en $1600) — más realista para posiciones concentradas

    # Límites derivados — calibrados para capital ~$1600 USD
    max_options_allocation: float = 0.20  # techo duro sobre el bucket options (= weight_options)
    max_single_option_premium: float = 250.0  # máx USD por contrato (1 contrato ~ 15% libro)
    max_hedge_allocation: float = 0.22    # hasta 22% del libro en puts SPY cuando bear (1 contrato SPY ~ $350)
    enable_short_equity: bool = False     # OFF por default — preferimos puts (riesgo limitado)
    enable_options: bool = True           # calls activados — apalancamiento definido sobre idea #1
    min_days_to_expiry: int = 14          # evita theta decay brutal
    max_days_to_expiry: int = 45          # evita gamma muerta (bajé de 60 a 45)
    target_delta_directional: float = 0.35  # puts/calls direccionales delta 0.35 (más OTM = más barato)
    target_delta_hedge: float = 0.22      # puts de hedge SPY bien OTM (prima baja, es un seguro)
    otm_fallback_steps: int = 3           # cuántos pasos 0.60x de delta probar si la prima excede cap


PARAMS = FinancialParams()


# ────────────────────────────────────────────────────────────────────────────
# RUTAS DE PROYECTO
# ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Paths:
    root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[1])

    @property
    def cache_dir(self) -> Path:
        p = self.root / ".cache"
        p.mkdir(exist_ok=True)
        return p

    @property
    def signals_dir(self) -> Path:
        p = self.root / "signals"
        p.mkdir(exist_ok=True)
        return p

    @property
    def logs_dir(self) -> Path:
        p = self.root / "logs"
        p.mkdir(exist_ok=True)
        return p


PATHS = Paths()


# ────────────────────────────────────────────────────────────────────────────
# NOTICIAS Y MACRO
# ────────────────────────────────────────────────────────────────────────────
# Tickers de Yahoo para indicadores macro — se consultan como si fueran activos
MACRO_TICKERS: dict[str, str] = {
    "oil_wti": "CL=F",         # Petróleo WTI
    "oil_brent": "BZ=F",       # Brent
    "gold": "GC=F",            # Oro
    "dxy": "DX-Y.NYB",         # Índice dólar
    "vix": "^VIX",             # Volatilidad esperada S&P500
    "us10y": "^TNX",           # Yield del 10y US Treasury
}

# Queries de Google News RSS para contexto global — se consultan una vez por run
MACRO_NEWS_QUERIES: list[str] = [
    "Trump economy tariffs",
    "Federal Reserve interest rates",
    "oil prices OPEC",
    "China economy",
    "Argentina Milei economy",
    "semiconductor export controls",
]

# Sentiment keyword-based (simple, sin ML). Se puede reemplazar por VADER o Gemini.
POSITIVE_KEYWORDS: set[str] = {
    "beat", "beats", "surge", "surges", "rally", "record", "upgrade", "upgraded",
    "buy", "bullish", "outperform", "growth", "strong", "jumps", "soars", "tops",
    "exceeds", "expansion", "approves", "approved", "wins", "profits", "profit",
}
NEGATIVE_KEYWORDS: set[str] = {
    "miss", "misses", "plunge", "plunges", "drop", "crash", "downgrade", "downgraded",
    "sell", "bearish", "underperform", "slump", "weak", "falls", "slides", "cuts",
    "warning", "layoffs", "bankruptcy", "probe", "lawsuit", "fraud", "delisted",
    "sanctions", "tariffs", "recession", "slowdown",
}


# ────────────────────────────────────────────────────────────────────────────
# IA / NOTIFICACIONES
# ────────────────────────────────────────────────────────────────────────────
# AI Ensemble — cada modelo para lo que hace mejor:
#   Haiku:  decisiones en tiempo real, clasificaciones rápidas ($0.25/M)
#   Sonnet: análisis profundo, SEC filings, tesis de inversión ($3/M)
#   Gemini: sentiment masivo, scan de 51 tickers, macro context (gratis/barato)
GEMINI_MODEL: str = "models/gemini-2.0-flash"
CLAUDE_MODEL: str = "claude-haiku-4-5-20251001"        # fast + cheap — risk debate, monitor
CLAUDE_MODEL_DEEP: str = "claude-sonnet-4-6"           # deep analysis — Wall St thesis, SEC
WHATSAPP_FROM: str = "whatsapp:+14155238886"  # Twilio sandbox
