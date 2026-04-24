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
    # Argentina (NYSE ADRs y locales)
    "Galicia": "GGAL", "Macro": "BMA", "TGS": "TGS", "Edenor": "EDN",
    "MercadoLibre": "MELI", "Despegar": "DESP", "IRSA": "IRS", "Aluar": "ALUA.BA",
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
    # ETFs / refugio
    "SPY": "ETF", "QQQ": "ETF", "GLD": "ETF", "IBIT": "Crypto", "ETHE": "Crypto",
}

# Máximo % del libro LP por sector (guard de diversificación)
MAX_SECTOR_WEIGHT_LP: float = 0.40      # ningún sector > 40% del sleeve LP
# Correlación máxima permitida entre dos activos LP (rolling 1y)
MAX_PAIR_CORRELATION: float = 0.72


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

    # Sleeves de asignación — dos buckets (opciones eliminadas)
    # 70% LP long (equity high conviction, 5 posiciones)
    # 30% CP long (equity trading técnico, momentum — más capital para noticias/AMD-style)
    weight_long_term: float = 0.70
    weight_short_term: float = 0.30
    weight_options: float = 0.00

    # Filtros de calidad para LP
    max_beta_lp: float = 1.5
    min_sharpe_lp: float = 0.55

    # Parámetros técnicos para CP
    rsi_oversold: float = 35.0
    rsi_overbought: float = 70.0
    atr_stop_multiple: float = 2.0        # stop loss = precio - 2*ATR

    # Concentración — 5 LP high conviction, 3 CP momentum
    top_n_long_term: int = 5
    top_n_short_term: int = 3

    # Bucket options
    top_n_bearish: int = 2                # hasta 2 puts direccionales simultáneos
    max_contracts_per_trade: int = 2      # 1-2 contratos por trade en paper

    # Restricciones de la optimización Markowitz
    max_weight_per_asset: float = 0.25    # hasta 25% por nombre (más diversificación)

    # Kill switch — el ejecutor no opera si el drawdown intradía supera este %
    max_daily_drawdown: float = 0.03      # 3%

    # Límites derivados — calibrados para capital ~$1600 USD
    max_options_allocation: float = 0.20  # techo duro sobre el bucket options (= weight_options)
    max_single_option_premium: float = 250.0  # máx USD por contrato (1 contrato ~ 15% libro)
    max_hedge_allocation: float = 0.22    # hasta 22% del libro en puts SPY cuando bear (1 contrato SPY ~ $350)
    enable_short_equity: bool = False     # OFF por default — preferimos puts (riesgo limitado)
    enable_options: bool = False          # opciones desactivadas — capital redirigido a equity
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
GEMINI_MODEL: str = "models/gemini-2.0-flash"
CLAUDE_MODEL: str = "claude-haiku-4-5-20251001"   # fast + cheap, $0.25/M tokens input
WHATSAPP_FROM: str = "whatsapp:+14155238886"  # Twilio sandbox
