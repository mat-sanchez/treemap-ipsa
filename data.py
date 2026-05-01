"""
Datos de mercado para el IPSA Treemap.

Contiene el universo de acciones, la descarga de precios desde Yahoo Finance
y la construcción del árbol jerárquico que consume D3.js.
"""

import json
import yfinance as yf
import pandas as pd
from datetime import timedelta, date


# ─────────────────────────────────────────────────────────────────────────────
# DICCIONARIO DE ACCIONES DEL IPSA
# ticker_base → (sector, nombre_empresa)
# Yahoo Finance requiere el sufijo ".SN" para la Bolsa de Santiago.
# ─────────────────────────────────────────────────────────────────────────────
IPSA = {
    "CHILE":      ("Banca y Servicios Financieros", "Banco de Chile"),
    "BSANTANDER": ("Banca y Servicios Financieros", "Banco Santander-Chile"),
    "BCI":        ("Banca y Servicios Financieros", "Banco de Credito e Inversiones"),
    "ITAUCL":     ("Banca y Servicios Financieros", "Banco Itau Chile"),
    "FALABELLA":  ("Retail",                        "Falabella S.A."),
    "CENCOSUD":   ("Retail",                        "Cencosud S.A."),
    "SMU":        ("Retail",                        "SMU S.A."),
    "RIPLEY":     ("Retail",                        "Ripley Corp S.A."),
    "MALLPLAZA":  ("Retail e Inmobiliario",         "Plaza S.A."),
    "PARAUCO":    ("Retail e Inmobiliario",         "Parque Arauco S.A."),
    "CENCOMALLS": ("Retail e Inmobiliario",         "Cencosud Shopping S.A."),
    "COPEC":      ("Energia y Combustibles",        "Empresas Copec S.A."),
    "ECL":        ("Energia y Combustibles",        "Engie Energia Chile S.A."),
    "ENELCHILE":  ("Generacion Electrica",          "Enel Chile S.A."),
    "ENELAM":     ("Generacion Electrica",          "Enel Americas S.A."),
    "COLBUN":     ("Generacion Electrica",          "Colbun S.A."),
    "SQM-B":      ("Mineria y Litio",               "SQM Serie B"),
    "CAP":        ("Mineria y Litio",               "CAP S.A."),
    "CMPC":       ("Celulosa y Papel",              "Empresas CMPC S.A."),
    "ANDINA-B":   ("Bebidas y Alimentos",           "Embotelladora Andina B"),
    "CCU":        ("Bebidas y Alimentos",           "Cias. Cerv. Unidas S.A."),
    "CONCHATORO": ("Vitivinicola",                  "Vina Concha y Toro"),
    "LTM":        ("Transporte Aereo",              "LATAM Airlines Group"),
    "VAPORES":    ("Transporte Maritimo",           "CSAV"),
    "ENTEL":      ("Telecomunicaciones",            "Entel S.A."),
    "QUINENCO":   ("Holding Industrial",            "Quinenco S.A."),
    "ILC":        ("Holding Financiero",            "Inversiones La Construccion"),
    "AGUAS-A":    ("Servicios Sanitarios",          "Aguas Andinas Serie A"),
    "IAM":        ("Servicios Sanitarios",          "Inv. Aguas Metropolitanas"),
    "SALFACORP":  ("Construccion",                  "Salfacorp S.A."),
}

# Lista de tickers en formato Yahoo Finance (se construye una sola vez)
TICKERS_YF = [f"{t}.SN" for t in IPSA]


# ─────────────────────────────────────────────────────────────────────────────
# FUNCIONES DE DESCARGA Y PROCESAMIENTO
# ─────────────────────────────────────────────────────────────────────────────

def download_prices(dl_start: str, dl_end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Descarga precios de cierre nominales y ajustados por dividendos para todos
    los tickers del IPSA desde Yahoo Finance.

    dl_start, dl_end: strings YYYY-MM-DD.
      · dl_start debe incluir un margen de ~7 días antes del inicio de análisis
        para cubrir fines de semana y festivos en días hábiles.
      · dl_end debe ser dl_end_real + 1 día porque yfinance excluye el día final
        (el intervalo es [dl_start, dl_end), semiabierto por la derecha).

    Retorna (close_df, adj_close_df): DataFrames con índice = fechas, columnas = tickers .SN
      · close_df     — precio de cierre nominal (sin ajuste de dividendos)
      · adj_close_df — precio de cierre ajustado por dividendos (Adj Close de Yahoo Finance)
    """
    raw = yf.download(
        TICKERS_YF,
        start=dl_start,
        end=dl_end,
        auto_adjust=False,  # False para obtener Close y Adj Close por separado
        progress=False,     # oculta barra de progreso de yfinance en consola
    )
    # yfinance retorna MultiIndex en columnas cuando descarga múltiples tickers:
    # nivel 0 = tipo de dato (Open, High, Low, Adj Close, Close, Volume)
    # nivel 1 = ticker
    if isinstance(raw.columns, pd.MultiIndex):
        return raw["Close"], raw["Adj Close"]
    return raw[["Close"]], raw[["Adj Close"]]


def build_hierarchy(close: pd.DataFrame, adj_close: pd.DataFrame,
                    ref_start: str, ref_end: str) -> dict:
    """
    Construye el árbol jerárquico que D3.hierarchy() necesita para el treemap.

    Estructura resultante:
      { name: "IPSA",
        children: [
          { name: "Sector",
            children: [
              { name: "TICKER",
                fullName: "...",
                shares: 123456,
                prices:    {"2024-01-02": 98.5, ...},  # cierres nominales (sin dividendos)
                adjPrices: {"2024-01-02": 91.3, ...},  # cierres ajustados por dividendos
                value: 1234567890,       # market_cap = shares × cierre nominal final
                pct: 2.34,              # variación % nominal (cierre previo → cierre final)
                price: 105.2,           # precio de cierre nominal al final del rango
              }, ...
            ]
          }, ...
        ]
      }

    ref_start, ref_end: rango de referencia para calcular pct y value iniciales.
    Soporta ref_start == ref_end (un solo día): pct es cierre de ese día vs. cierre del día hábil anterior.
    El HTML puede recalcular para cualquier sub-rango usando 'prices' o 'adjPrices'.
    """
    hierarchy: dict = {}

    for ticker, (industry, name) in IPSA.items():
        yf_ticker = f"{ticker}.SN"

        # ── Extraer y limpiar series de precios ──────────────────────────────
        try:
            close_series     = close[yf_ticker].dropna()
            adj_close_series = adj_close[yf_ticker].dropna()
        except KeyError:
            continue
        if len(close_series) < 2:
            continue

        # Convertir a dict {string_fecha: float_precio}
        prices_dict: dict = {
            d.strftime("%Y-%m-%d"): round(float(p), 2)
            for d, p in close_series.items()
        }
        adj_prices_dict: dict = {
            d.strftime("%Y-%m-%d"): round(float(p), 2)
            for d, p in adj_close_series.items()
        }

        # ── Obtener acciones en circulación ──────────────────────────────────
        # fast_info es más rápido que .info (no descarga metadata completa)
        shares = 0
        try:
            shares = int(yf.Ticker(yf_ticker).fast_info.shares or 0)
        except Exception:
            shares = 0   # fallback: JS usará precio × 1M como estimación

        # ── Calcular variación % nominal para el rango de referencia ─────────
        all_dates = sorted(prices_dict.keys())

        # Último día hábil anterior a ref_start (cierre de referencia base)
        dates_before_start = [d for d in all_dates if d < ref_start]
        # Último día hábil disponible con fecha <= ref_end
        dates_to_end       = [d for d in all_dates if d <= ref_end]

        if not dates_before_start or not dates_to_end:
            continue

        d_ini = dates_before_start[-1]  # último día hábil previo al inicio
        d_fin = dates_to_end[-1]        # fecha efectiva de término

        p_ini = prices_dict[d_ini]
        p_fin = prices_dict[d_fin]

        pct_change = round((p_fin / p_ini - 1) * 100, 2)
        market_cap = shares * p_fin if shares > 0 else p_fin * 1_000_000

        # ── Agregar al árbol de jerarquía ────────────────────────────────────
        hierarchy.setdefault(industry, []).append({
            "name":      ticker,
            "fullName":  name,
            "shares":    shares,
            "prices":    prices_dict,      # nominales → modo por defecto
            "adjPrices": adj_prices_dict,  # ajustados por dividendos → modo alternativo
            "value":     market_cap,
            "pct":       pct_change,
            "price":     p_fin,
        })

    return {
        "name": "IPSA",
        "children": [
            {"name": sector, "children": tickers}
            for sector, tickers in hierarchy.items()
        ],
    }


def refresh_and_build_json() -> str:
    """
    Descarga 13 meses frescos de precios desde Yahoo Finance y retorna el árbol
    D3 completo serializado como string JSON.

    Llamada desde el endpoint HTTP GET /refresh al cargarse la página (auto-refresh).
    Usa el mismo rango que la descarga inicial (400 días calendario ≈ 13 meses)
    para que el caché del navegador quede completamente actualizado y el filtro
    local siga funcionando sobre datos recientes.
    """
    t_today   = date.today()
    r_start   = (t_today - timedelta(days=400)).strftime("%Y-%m-%d")
    r_end     = (t_today + timedelta(days=2)).strftime("%Y-%m-%d")   # +2: intervalo semiabierto
    # ref_start distinto de r_start: necesitamos fechas anteriores a ref_start en los datos.
    # Usar la última semana igual que el arranque inicial.
    ref_start = (t_today - timedelta(days=7)).strftime("%Y-%m-%d")

    close, adj_close = download_prices(r_start, r_end)
    tree = build_hierarchy(close, adj_close, ref_start, t_today.strftime("%Y-%m-%d"))
    # Escapar </ para que el JSON pueda embeberse en <script> sin riesgo de break-out
    return json.dumps(tree, ensure_ascii=False).replace("</", r"<\/")
