"""
Treemap IPSA — Capitalización de Mercado por Industria
Servidor HTTP local con actualizaciones en vivo desde el navegador.

Flujo completo:
  1. Al iniciar, descarga 13 meses de precios diarios del IPSA desde Yahoo Finance.
  2. Levanta un servidor HTTP local (autodetecta un puerto libre desde el 8765).
  3. Abre el navegador apuntando a http://localhost:PORT/.
  4. El HTML carga con los 13 meses de data embebida.
     - Cambiar las fechas en el panel de filtro → recalculo instantáneo (sin red).
     - Clic en "Filtrar"    → recalculo local con los 13 meses embebidos (sin red).
     - Clic en "Actualizar" → descarga 13 meses frescos desde Yahoo Finance y
                              reemplaza el caché completo del navegador.
     - Clic en "⬇ PNG"     → exporta la vista actual (header + filtros + treemap
                             + leyenda) como imagen PNG de alta resolución.
  5. El servidor corre indefinidamente; se detiene con Ctrl+C en la consola.
"""

import os
import sys
import io
import json
import webbrowser
import socket
import threading
import urllib.parse
import http.server
import socketserver

# Forzar UTF-8 en consola Windows para evitar errores con tildes y ñ
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import yfinance as yf
import pandas as pd
from datetime import timedelta, date


# ─────────────────────────────────────────────────────────────────────────────
# 1. DICCIONARIO DE ACCIONES DEL IPSA
#    ticker_base → (sector, nombre_empresa)
#    Yahoo Finance requiere el sufijo ".SN" para la Bolsa de Santiago.
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
# 2. FUNCIONES DE DESCARGA Y PROCESAMIENTO
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

    Llamada desde el endpoint HTTP GET /refresh cuando el usuario pulsa "Actualizar".
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
    return json.dumps(tree, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# 3. DESCARGA INICIAL — 13 MESES DE DATOS DIARIOS
#    Esta descarga se hace una sola vez al arrancar el script.
#    Los precios de los últimos 13 meses se embeben en el HTML para que
#    el filtro de fechas local en el navegador funcione sin red.
#    400 días calendario ≈ 13 meses (cubre ~280 días hábiles de bolsa).
# ─────────────────────────────────────────────────────────────────────────────

today = date.today()

# Rango de descarga: 13 meses hacia atrás con margen de 2 días hacia adelante
hist_start = (today - timedelta(days=400)).strftime("%Y-%m-%d")
hist_end   = (today + timedelta(days=2)).strftime("%Y-%m-%d")   # +2 por intervalo semiabierto

print("=" * 62)
print("   IPSA Treemap  —  Visualizador del mercado chileno")
print("=" * 62)
print()
print(f"  Descargando 13 meses de datos diarios")
print(f"  ({hist_start} → {today})  ·  {len(IPSA)} tickers  ...")

close_full, adj_close_full = download_prices(hist_start, hist_end)
print(f"  {len(close_full)} dias de trading descargados\n")

# Rango de visualización inicial: última semana
# El usuario puede cambiar esto libremente desde el panel del navegador
default_end   = today.strftime("%Y-%m-%d")
default_start = (today - timedelta(days=7)).strftime("%Y-%m-%d")

# Fechas extremas disponibles en los datos descargados (para los date pickers)
date_min = hist_start    # fecha mínima permitida en el filtro del HTML
date_max = today.strftime("%Y-%m-%d")   # fecha máxima = hoy

# Construir árbol D3 con datos iniciales (vista de última semana)
print("  Construyendo jerarquía inicial ...")
d3_tree   = build_hierarchy(close_full, adj_close_full, default_start, default_end)
data_json = json.dumps(d3_tree, ensure_ascii=False)

total_init = sum(len(s["children"]) for s in d3_tree["children"])
print(f"  {total_init}/{len(IPSA)} tickers incluidos en el rango inicial\n")


# ─────────────────────────────────────────────────────────────────────────────
# 4. PLANTILLA HTML CON D3.js
#    El HTML se sirve desde el servidor HTTP local (no como archivo).
#    Esto permite que el botón "Actualizar" haga fetch("/refresh")
#    al mismo origen (localhost), sin problemas de CORS.
#
#    Placeholders reemplazados por Python antes de servir:
#      DATA_PLACEHOLDER      → JSON con 13 meses de precios
#      DEFAULT_START         → fecha inicio vista inicial (YYYY-MM-DD)
#      DEFAULT_END           → fecha fin vista inicial (YYYY-MM-DD)
#      DATE_MIN_PLACEHOLDER  → fecha mínima para date pickers
#      DATE_MAX_PLACEHOLDER  → fecha máxima para date pickers
# ─────────────────────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IPSA Treemap</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700;800&display=swap" rel="stylesheet">
<script src="https://d3js.org/d3.v7.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<style>
  /* Reset: modelo de caja predecible en todos los elementos */
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  /* Layout de columna vertical que ocupa toda la pantalla sin scroll */
  body {
    background: #0d1117;
    color: #e6edf3;
    font-family: 'Inter', sans-serif;
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
  }

  /* ── Header ─────────────────────────────────────────────────────────── */
  #header {
    padding: 14px 24px;
    border-bottom: 1px solid #1e2533;
    display: flex;
    align-items: center;
    gap: 14px;
    flex-shrink: 0;
    background: linear-gradient(to bottom, #111827, #0d1117);
  }
  #header h1          { font-size: 17px; font-weight: 800; letter-spacing: -0.4px; color: #f0f6fc; }
  #header .dot        { color: #3d4f6e; }
  #header .subtitle   { font-size: 12px; color: #4a5568; font-weight: 400; }

  /* Badge que muestra el período activo; JS lo actualiza al cambiar fechas */
  .badge {
    margin-left: auto;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    padding: 3px 10px;
    border-radius: 20px;
    border: 1px solid #2d3748;
    color: #7c8fa6;
    background: #111827;
    white-space: nowrap;
  }

  /* ── Panel de filtro de fechas ────────────────────────────────────────── */
  /* Barra entre el header y el treemap con los controles de rango */
  #date-filter {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 9px 24px;
    background: #0f1621;
    border-bottom: 1px solid #1e2533;
    flex-shrink: 0;
    flex-wrap: wrap;
  }

  /* Etiqueta "Período" a la izquierda */
  #filter-label {
    font-size: 11px;
    font-weight: 700;
    color: #4a5568;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    white-space: nowrap;
  }

  /* Inputs de fecha con estilo de chip/etiqueta redondeada */
  .date-chip {
    background: #161b22;
    border: 1px solid #2d3748;
    border-radius: 20px;
    color: #c9d1d9;
    font-family: 'Inter', sans-serif;
    font-size: 12px;
    font-weight: 600;
    padding: 5px 14px;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
    outline: none;
    color-scheme: dark;   /* activa el tema oscuro en el date picker nativo del navegador */
  }
  .date-chip:hover { border-color: #4a5568; background: #1a2030; }
  .date-chip:focus { border-color: #58a6ff; background: #1a2030; }

  /* Separador visual entre los dos date pickers */
  .filter-arrow { color: #3d4f6e; font-size: 14px; user-select: none; }

  /* Botón principal "Actualizar" — descarga datos frescos de yfinance */
  #btn-update {
    background: #1f3a5c;
    border: 1px solid #2a5298;
    border-radius: 20px;
    color: #79b8ff;
    font-family: 'Inter', sans-serif;
    font-size: 11px;
    font-weight: 700;
    padding: 5px 16px;
    cursor: pointer;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    transition: background 0.15s, border-color 0.15s, opacity 0.15s;
    white-space: nowrap;
  }
  #btn-update:hover      { background: #2a4d7a; border-color: #58a6ff; }
  #btn-update:active     { background: #1a3050; }
  #btn-update:disabled   { opacity: 0.45; cursor: not-allowed; }

  /* Botón secundario "Filtrar" — recalculo local sin red */
  #btn-local {
    background: transparent;
    border: 1px solid #2d3748;
    border-radius: 20px;
    color: #4a5568;
    font-family: 'Inter', sans-serif;
    font-size: 11px;
    font-weight: 600;
    padding: 5px 14px;
    cursor: pointer;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    transition: border-color 0.15s, color 0.15s;
    white-space: nowrap;
  }
  #btn-local:hover { border-color: #4a5568; color: #7c8fa6; }

  /* Botón de descarga PNG */
  #btn-download {
    background: transparent;
    border: 1px solid #2d3748;
    border-radius: 20px;
    color: #4a5568;
    font-family: 'Inter', sans-serif;
    font-size: 11px;
    font-weight: 600;
    padding: 5px 14px;
    cursor: pointer;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    transition: border-color 0.15s, color 0.15s;
    white-space: nowrap;
  }
  #btn-download:hover    { border-color: #4a5568; color: #7c8fa6; }
  #btn-download:disabled { opacity: 0.45; cursor: not-allowed; }

  /* ── Toggle nominal / ajustado por dividendos ───────────────────────────── */
  .adj-toggle-wrap {
    display: flex;
    align-items: center;
    gap: 7px;
    cursor: pointer;
    user-select: none;
    margin-left: 16px;
  }
  .adj-toggle-lbl {
    font-size: 11px;
    color: #3a4a5a;
    white-space: nowrap;
    transition: color 0.2s;
  }
  .adj-toggle-lbl.on { color: #c8d8e8; }
  #adj-toggle {
    position: relative;
    width: 48px;
    height: 26px;
    background: #0d1117;
    border: 1px solid #2d3748;
    border-radius: 999px;
    flex-shrink: 0;
    transition: border-color 0.25s;
  }
  #adj-toggle:hover { border-color: #4a5568; }
  #adj-toggle.active { border-color: #7c5cbf; }
  .toggle-knob {
    position: absolute;
    top: 3px;
    left: 3px;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: linear-gradient(135deg, #4facfe 0%, #a78bfa 45%, #f472b6 100%);
    box-shadow: 0 0 7px rgba(167, 139, 250, 0.55);
    transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
  }
  #adj-toggle.active .toggle-knob { transform: translateX(22px); }

  /* Mensaje de estado debajo del filtro (loading / ok / error) */
  #filter-status {
    font-size: 11px;
    color: #4a5568;
    font-style: italic;
    transition: color 0.25s;
    min-width: 120px;
  }
  #filter-status.loading { color: #58a6ff; }
  #filter-status.ok      { color: #3fb950; }
  #filter-status.error   { color: #f85149; }

  /* ── Barra de progreso (visible solo durante descarga de /refresh) ──────── */
  /* Ocupa 3px de alto; se muestra/oculta por JS con display block/none */
  #progress-bar-wrap {
    height: 3px;
    background: #1e2533;
    flex-shrink: 0;
    display: none;
    overflow: hidden;
  }
  /* Animación deslizante infinita mientras dura la descarga */
  #progress-bar-fill {
    height: 100%;
    width: 40%;
    background: linear-gradient(90deg, #1f3a5c, #58a6ff, #1f3a5c);
    background-size: 200% 100%;
    animation: progress-slide 1.4s infinite linear;
  }
  @keyframes progress-slide {
    0%   { margin-left: -40%; }
    100% { margin-left: 100%; }
  }

  /* ── Área del treemap ─────────────────────────────────────────────────── */
  /* flex:1 hace que tome todo el espacio vertical disponible */
  #chart-container { flex: 1; position: relative; overflow: hidden; }
  svg { width: 100%; height: 100%; display: block; }

  /* ── Texto dentro del SVG ─────────────────────────────────────────────── */
  /* Etiqueta del sector en la esquina superior de cada grupo */
  .industry-label {
    font-family: 'Inter', sans-serif;
    font-size: 9px; font-weight: 700;
    fill: rgba(230,237,243,0.55);
    text-transform: uppercase; letter-spacing: 0.9px;
    pointer-events: none;   /* no intercepta eventos de mouse */
  }
  /* Nombre del ticker dentro de cada celda */
  .ticker-name {
    font-family: 'Inter', sans-serif; font-weight: 800;
    fill: rgba(255,255,255,0.95);
    pointer-events: none; text-anchor: middle; dominant-baseline: middle;
  }
  /* Variación % debajo del nombre del ticker */
  .ticker-pct {
    font-family: 'Inter', sans-serif; font-weight: 500;
    fill: rgba(255,255,255,0.75);
    pointer-events: none; text-anchor: middle; dominant-baseline: middle;
  }

  /* ── Tooltip ──────────────────────────────────────────────────────────── */
  /* Aparece al hacer hover sobre una celda; z-index alto para ir sobre el SVG */
  #tooltip {
    position: fixed;
    background: #161b22; border: 1px solid #2d3748;
    border-radius: 10px; padding: 14px 18px;
    font-size: 12px; pointer-events: none;
    opacity: 0; transition: opacity 0.12s ease;
    min-width: 200px;
    box-shadow: 0 16px 40px rgba(0,0,0,0.6), 0 0 0 1px rgba(255,255,255,0.04);
    z-index: 999;
  }
  #tooltip.visible { opacity: 1; }
  .tip-ticker { font-size: 16px; font-weight: 800; color: #f0f6fc; margin-bottom: 1px; }
  .tip-name   { font-size: 10px; color: #5a6a7e; margin-bottom: 10px; font-weight: 400; }
  .tip-sep    { height: 1px; background: #1e2533; margin-bottom: 8px; }
  .tip-row    { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 5px; color: #4a5568; font-size: 11px; }
  .tip-val    { color: #c9d1d9; font-weight: 600; font-size: 12px; }
  .pos        { color: #3fb950 !important; }
  .neg        { color: #f85149 !important; }

  /* ── Leyenda inferior ────────────────────────────────────────────────── */
  #legend {
    padding: 8px 24px 10px; border-top: 1px solid #1e2533;
    display: flex; align-items: center; gap: 12px;
    flex-shrink: 0; background: #0d1117;
  }
  #legend-label { font-size: 10px; color: #4a5568; font-weight: 600; text-transform: uppercase; letter-spacing: 0.6px; }
  #legend-wrap  { display: flex; flex-direction: column; gap: 3px; }
  #legend-bar   { border-radius: 3px; }
  #legend-ticks { display: flex; justify-content: space-between; font-size: 9px; color: #4a5568; font-weight: 600; }
  #legend-right { margin-left: auto; display: inline-flex; align-items: center; gap: 10px; }
  #legend-date  { font-size: 10px; color: #3d4f6e; }
  #linkedin-link {
    font-size: 9px; color: #3d4f6e; text-decoration: none; font-weight: 600;
    display: inline-flex; align-items: center; gap: 3px;
  }
  #linkedin-link:hover { color: #74b0e8; text-decoration: underline; }
</style>
</head>
<body>

<!-- ── Header ──────────────────────────────────────────────────────────────── -->
<div id="header">
  <h1>IPSA</h1>
  <span class="dot">·</span>
  <span class="subtitle">Capitalización de mercado por industria · Área = Market Cap</span>
  <!-- El badge muestra el período activo; se actualiza por JS -->
  <span class="badge" id="period-badge">—</span>

  <!-- Toggle nominal / ajustado por dividendos -->
  <div class="adj-toggle-wrap" onclick="toggleAdjusted()" title="Cambiar entre precios nominales y ajustados por dividendos">
    <span class="adj-toggle-lbl on" id="lbl-nominal">Nominal</span>
    <div id="adj-toggle"><div class="toggle-knob"></div></div>
    <span class="adj-toggle-lbl" id="lbl-adj">Ajustado por Dividendos</span>
  </div>
</div>

<!-- ── Panel de filtro de fechas ────────────────────────────────────────────── -->
<!--
  Tres modos de acción:
    1. "Filtrar"    (btn-local):    recalculo instantáneo con los 13 meses embebidos.
    2. "Actualizar" (btn-update):   fetch("/refresh") → descarga 13 meses frescos →
                                    reemplaza rawData completo → reaplica el filtro.
    3. "⬇ PNG"     (btn-download): captura la vista con html2canvas y la descarga.
-->
<div id="date-filter">
  <span id="filter-label">Período</span>

  <!-- Date picker inicio: inicializado con la vista por defecto -->
  <input type="date" id="date-start" class="date-chip"
         value="DEFAULT_START"
         min="DATE_MIN_PLACEHOLDER"
         max="DATE_MAX_PLACEHOLDER"
         title="Fecha de inicio">

  <span class="filter-arrow">→</span>

  <!-- Date picker término -->
  <input type="date" id="date-end" class="date-chip"
         value="DEFAULT_END"
         min="DATE_MIN_PLACEHOLDER"
         max="DATE_MAX_PLACEHOLDER"
         title="Fecha de término">

  <!-- Filtrar local: usa los 13 meses ya descargados (instantáneo, sin red) -->
  <button id="btn-local"  onclick="localFilter()">Filtrar</button>

  <!-- Actualizar: descarga datos frescos de yfinance para el rango elegido -->
  <button id="btn-update" onclick="freshUpdate()">↓ Actualizar datos</button>

  <!-- Mensaje de estado que aparece durante descarga, ok o error -->
  <span id="filter-status"></span>

  <!-- Descargar la vista actual como imagen PNG -->
  <button id="btn-download" onclick="downloadPNG()" title="Descargar imagen PNG de la vista actual">⬇ PNG</button>
</div>

<!-- ── Barra de progreso (visible solo durante "Actualizar") ────────────────── -->
<div id="progress-bar-wrap">
  <div id="progress-bar-fill"></div>
</div>

<!-- ── Treemap SVG ───────────────────────────────────────────────────────────── -->
<div id="chart-container">
  <svg id="treemap"></svg>
</div>

<div id="tooltip"></div>

<!-- ── Leyenda de color ──────────────────────────────────────────────────────── -->
<div id="legend">
  <span id="legend-label">Variación %</span>
  <div id="legend-wrap">
    <canvas id="legend-bar" width="220" height="7"></canvas>
    <div id="legend-ticks">
      <span>-5%</span><span>-2.5%</span><span>0%</span><span>+2.5%</span><span>+5%</span>
    </div>
  </div>
  <span id="legend-right">
    <span id="legend-date"></span>
    <a id="linkedin-link" href="https://www.linkedin.com/in/mat%C3%ADas-alejandro-s%C3%A1nchez-ruiz/" target="_blank" rel="noopener">
      <svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" viewBox="0 0 24 24" fill="currentColor">
        <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 0 1-2.063-2.065 2.064 2.064 0 1 1 2.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/>
      </svg>
      Matías Sánchez Ruiz
    </a>
  </span>
</div>

<script>
// ════════════════════════════════════════════════════════════════════════════════
// DATOS EMBEBIDOS (generados por Python al arrancar el servidor)
//
// rawData contiene el árbol jerárquico completo con 13 meses de precios diarios.
// Cada nodo hoja (ticker) tiene:
//   · prices:    { "YYYY-MM-DD": precio_float, ... }  — cierres nominales
//   · adjPrices: { "YYYY-MM-DD": precio_float, ... }  — cierres ajustados por dividendos
//   · shares:    acciones en circulación (para recalcular market cap)
//   · value, pct, price: calculados para la vista inicial (última semana, modo nominal)
//
// rawData actúa como caché base para el filtro local (sin red).
// Al pulsar "Actualizar", rawData.children se reemplaza completo con 13 meses
// frescos descargados desde Yahoo Finance (endpoint /refresh).
// ════════════════════════════════════════════════════════════════════════════════
const rawData = DATA_PLACEHOLDER;

// Rango de fechas de la vista inicial (inyectado por Python)
const defaultStart = "DEFAULT_START";
const defaultEnd   = "DEFAULT_END";

// currentData es el árbol calculado que se pasa a D3 para renderizar.
// Empieza con el rango inicial y se reemplaza cada vez que el usuario filtra.
let currentData = null;

// useAdjusted: si true, computeDataForRange usa adjPrices (ajustado por dividendos)
// en lugar de prices (nominal). Controlado por el toggle "Nominal / Ajustado por Dividendos" del header.
let useAdjusted = false;


// ════════════════════════════════════════════════════════════════════════════════
// ESCALA DE COLOR CUADRÁTICA DIVERGENTE
//
// Convierte variación % en un color RGB:
//   Negativo →  rojo oscuro  (pérdida)
//   Cero     →  azul pizarra (neutro)
//   Positivo →  verde        (ganancia)
//
// Usamos potencia 1.8 (casi cuadrática) para que:
//   · Cambios pequeños (±0.5%) sean suaves.
//   · Cambios grandes (±5%) sean muy vívidos.
// MAX_PCT: umbral de saturación — más allá de este valor el color no cambia.
// ════════════════════════════════════════════════════════════════════════════════
const NEG_COLOR_MAX = "#e5383b";   // rojo intenso (pérdida grande)
const NEU_COLOR     = "#1a2030";   // azul-gris neutro (sin cambio)
const POS_COLOR_MAX = "#2ecc71";   // verde intenso (ganancia grande)
const MAX_PCT = 5.0;

function quadColor(pct) {
  // Recortar al rango [-1, 1] y aplicar curva de potencia
  const n    = Math.max(-1, Math.min(1, pct / MAX_PCT));
  const quad = Math.sign(n) * Math.pow(Math.abs(n), 1.8);
  // Interpolar entre neutro y el color extremo correspondiente
  return quad >= 0
    ? d3.interpolateRgb(NEU_COLOR, POS_COLOR_MAX)(quad)
    : d3.interpolateRgb(NEU_COLOR, NEG_COLOR_MAX)(Math.abs(quad));
}


// ════════════════════════════════════════════════════════════════════════════════
// LEYENDA DE GRADIENTE (dibujada en el <canvas> del footer)
// Muestreamos 41 puntos de quadColor de -MAX_PCT a +MAX_PCT para el gradiente.
// ════════════════════════════════════════════════════════════════════════════════
(function drawLegend() {
  const canvas = document.getElementById("legend-bar");
  const ctx    = canvas.getContext("2d");
  const grad   = ctx.createLinearGradient(0, 0, canvas.width, 0);
  for (let i = 0; i <= 40; i++) {
    const t   = i / 40;
    const pct = -MAX_PCT + t * (MAX_PCT * 2);
    grad.addColorStop(t, quadColor(pct));
  }
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
})();

// Mostrar fecha de generación del HTML en el footer
document.getElementById("legend-date").textContent =
  "Generado: " + new Date().toLocaleDateString("es-CL", {
    day: "2-digit", month: "short", year: "numeric"
  });


// ════════════════════════════════════════════════════════════════════════════════
// RECALCULAR DATOS PARA UN RANGO DE FECHAS
//
// Construye un nuevo árbol D3 a partir de rawData usando solo las fechas
// dentro de [startStr, endStr]. Esta operación es puramente local (sin red).
//
// Para cada ticker:
//   · precio_inicio = cierre del último día hábil anterior a startStr
//   · precio_fin    = cierre del último día hábil con fecha <= endStr
//   · pct           = (precio_fin / precio_inicio - 1) × 100
//   · market_cap    = shares × cierre nominal final (siempre nominal para el área)
// Soporta startStr == endStr (un solo día): pct es ese cierre vs. el cierre del día hábil anterior.
// La serie usada para pct (nominal o ajustada por dividendos) depende de useAdjusted.
//
// Tickers sin datos en el rango se excluyen del árbol resultante.
// Sectores que quedan vacíos también se excluyen.
// ════════════════════════════════════════════════════════════════════════════════
function computeDataForRange(startStr, endStr) {
  const newSectors = rawData.children.map(sector => {
    const newTickers = sector.children.map(ticker => {
      const priceData = useAdjusted ? ticker.adjPrices : ticker.prices;
      const dates = Object.keys(priceData).sort();

      // Buscar fechas efectivas de inicio y término
      const beforeStart = dates.filter(d => d < startStr);  // días hábiles anteriores al inicio
      const toEnd       = dates.filter(d => d <= endStr);

      if (!beforeStart.length || !toEnd.length) return null;  // sin cierre base o sin datos al fin

      const dIni = beforeStart[beforeStart.length - 1];  // último día hábil previo al inicio
      const dFin = toEnd[toEnd.length - 1];
      const pIni = priceData[dIni];
      const pFin = priceData[dFin];

      const pct = +((pFin / pIni - 1) * 100).toFixed(2);
      // Market cap siempre con precio nominal para que el área refleje el valor de mercado real
      const nominalFin = ticker.prices[dFin] ?? pFin;
      const mcap = ticker.shares > 0 ? ticker.shares * nominalFin : nominalFin * 1e6;

      // Retornar nodo hoja con valores recalculados (shares, prices y adjPrices se mantienen)
      return { ...ticker, pct, price: nominalFin, value: mcap };
    }).filter(Boolean);  // eliminar nulls

    return { name: sector.name, children: newTickers };
  }).filter(s => s.children.length > 0);  // eliminar sectores vacíos

  return { name: "IPSA", children: newSectors };
}


// ════════════════════════════════════════════════════════════════════════════════
// UTILIDADES DE UI
// ════════════════════════════════════════════════════════════════════════════════

function showProgress() {
  document.getElementById("progress-bar-wrap").style.display = "block";
}

function hideProgress() {
  document.getElementById("progress-bar-wrap").style.display = "none";
}

function setStatus(type, msg) {
  const el = document.getElementById("filter-status");
  el.textContent  = msg;
  el.className    = type;  // "loading" | "ok" | "error" | ""
}

function updateBadge(startStr, endStr) {
  // Formatea dos fechas ISO a texto legible y actualiza el badge del header
  const fmt = iso => new Date(iso + "T12:00:00").toLocaleDateString("es-CL", {
    day: "2-digit", month: "short", year: "numeric"
  });
  document.getElementById("period-badge").textContent =
    `Var. ${fmt(startStr)} → ${fmt(endStr)}`;
}

function validateDates() {
  // Lee y valida los date pickers; retorna {startVal, endVal} o null si hay error
  const startVal = document.getElementById("date-start").value;
  const endVal   = document.getElementById("date-end").value;
  if (!startVal || !endVal)   { setStatus("error", "⚠ Ingrese ambas fechas");                   return null; }
  if (startVal > endVal)      { setStatus("error", "⚠ Inicio no puede ser posterior al término"); return null; }
  return { startVal, endVal };
}

function countLeaves(tree) {
  // Cuenta el total de tickers (hojas) en el árbol D3
  return tree.children.reduce((acc, s) => acc + s.children.length, 0);
}


// ════════════════════════════════════════════════════════════════════════════════
// TOGGLE NOMINAL / AJUSTADO POR DIVIDENDOS
// ════════════════════════════════════════════════════════════════════════════════
function toggleAdjusted() {
  useAdjusted = !useAdjusted;

  document.getElementById("adj-toggle").classList.toggle("active", useAdjusted);
  document.getElementById("lbl-nominal").classList.toggle("on", !useAdjusted);
  document.getElementById("lbl-adj").classList.toggle("on",  useAdjusted);

  // Re-renderizar con el modo recién seleccionado y el rango actual
  const dates = validateDates();
  if (!dates) return;
  currentData = computeDataForRange(dates.startVal, dates.endVal);
  if (countLeaves(currentData) > 0) render(currentData);
}


// ════════════════════════════════════════════════════════════════════════════════
// FILTRO LOCAL — usa los 13 meses de data embebida (sin red)
// Llamado por el botón "Filtrar". Instantáneo.
// ════════════════════════════════════════════════════════════════════════════════
function localFilter() {
  const dates = validateDates();
  if (!dates) return;
  const { startVal, endVal } = dates;

  currentData = computeDataForRange(startVal, endVal);

  if (countLeaves(currentData) === 0) {
    setStatus("error", "⚠ Sin datos en ese rango");
    return;
  }

  updateBadge(startVal, endVal);
  setStatus("ok", `✓ ${countLeaves(currentData)} tickers · datos locales`);
  render(currentData);
}


// ════════════════════════════════════════════════════════════════════════════════
// ACTUALIZAR CON DATOS FRESCOS — descarga 13 meses nuevos desde Yahoo Finance
//
// Hace fetch("/refresh") al servidor Python local (sin parámetros).
// El servidor descarga los 13 meses completos y devuelve el árbol D3 completo.
// rawData.children se reemplaza íntegramente con el resultado, de modo que
// el filtro local también trabaje sobre precios actualizados.
// Finalmente se recalcula el rango elegido en los date pickers y se redibuja.
// ════════════════════════════════════════════════════════════════════════════════
async function freshUpdate() {
  const dates = validateDates();
  if (!dates) return;
  const { startVal, endVal } = dates;

  const btn = document.getElementById("btn-update");
  btn.disabled    = true;
  btn.textContent = "Descargando...";
  setStatus("loading", "⟳ Descargando 13 meses desde Yahoo Finance...");
  showProgress();

  try {
    // /refresh devuelve el árbol D3 completo con 13 meses de precios frescos
    const response = await fetch("/refresh");

    if (!response.ok) {
      const err = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
      throw new Error(err.error || `HTTP ${response.status}`);
    }

    const freshTree = await response.json();
    if (freshTree.error) throw new Error(freshTree.error);

    // Reemplazar el caché completo; a partir de aquí el filtro local usa data fresca
    rawData.children = freshTree.children;

    // Recalcular la vista para el rango elegido en los date pickers
    currentData = computeDataForRange(startVal, endVal);

    if (countLeaves(currentData) === 0) {
      setStatus("error", "⚠ Sin datos en ese rango");
      return;
    }

    updateBadge(startVal, endVal);
    setStatus("ok", `✓ ${countLeaves(currentData)} tickers · datos frescos`);
    render(currentData);

  } catch (err) {
    setStatus("error", `⚠ ${err.message}`);
  } finally {
    hideProgress();
    btn.disabled    = false;
    btn.textContent = "↓ Actualizar datos";
  }
}


// ════════════════════════════════════════════════════════════════════════════════
// RENDERIZADO DEL TREEMAP
//
// Recibe un árbol D3 y dibuja el treemap completo en el SVG.
// Se llama en la carga inicial y cada vez que cambia el rango de fechas.
//
// Estructura del treemap (D3 treemapSquarify):
//   · Nodos internos (sectores): fondo oscuro + etiqueta en la esquina superior.
//   · Nodos hoja (tickers): rectángulo coloreado + ticker + variación %.
//   · El área de cada celda es proporcional al market cap (campo 'value').
//   · El color depende de la variación % (función quadColor).
// ════════════════════════════════════════════════════════════════════════════════
const svg       = d3.select("#treemap");
const container = document.getElementById("chart-container");
const tooltip   = document.getElementById("tooltip");

function render(data) {
  svg.selectAll("*").remove();  // limpiar el SVG antes de redibujar

  const W = container.clientWidth;
  const H = container.clientHeight;
  svg.attr("viewBox", `0 0 ${W} ${H}`);

  // Construir jerarquía D3: suma los 'value' de hojas hacia arriba,
  // y ordena de mayor a menor para que los sectores grandes aparezcan primero
  const root = d3.hierarchy(data)
    .sum(d => d.value || 0)
    .sort((a, b) => b.value - a.value);

  // Calcular posiciones de los rectángulos
  // treemapSquarify.ratio(1.2): minimiza elongación de celdas (más cuadradas)
  // paddingTop(20): reserva espacio para la etiqueta del sector
  // paddingInner(2): separación de 2px entre celdas dentro de un sector
  d3.treemap()
    .tile(d3.treemapSquarify.ratio(1.2))
    .size([W, H])
    .paddingOuter(5)
    .paddingTop(20)
    .paddingInner(2)
    (root);

  // ── Grupos de sector (nodos nivel 1) ────────────────────────────────────
  const groups = svg.selectAll(".ig").data(root.children).join("g").attr("class", "ig");

  // Fondo oscuro del grupo de sector
  groups.append("rect")
    .attr("x", d => d.x0).attr("y", d => d.y0)
    .attr("width",  d => Math.max(0, d.x1 - d.x0))
    .attr("height", d => Math.max(0, d.y1 - d.y0))
    .attr("fill", "#10151e").attr("rx", 4);

  // Nombre del sector truncado si el grupo es demasiado angosto
  groups.append("text")
    .attr("class", "industry-label")
    .attr("x", d => d.x0 + 7).attr("y", d => d.y0 + 13)
    .text(d => {
      const maxChars = Math.floor((d.x1 - d.x0) / 6.5);
      return d.data.name.length > maxChars
        ? d.data.name.substring(0, maxChars - 1) + "…"
        : d.data.name;
    });

  // ── Celdas individuales (hojas = tickers) ───────────────────────────────
  const cells = svg.selectAll(".cell").data(root.leaves()).join("g")
    .attr("class", "cell").style("cursor", "pointer");

  // Rectángulo coloreado según variación %
  cells.append("rect")
    .attr("x", d => d.x0 + 1).attr("y", d => d.y0 + 1)
    .attr("width",  d => Math.max(0, d.x1 - d.x0 - 2))
    .attr("height", d => Math.max(0, d.y1 - d.y0 - 2))
    .attr("fill", d => quadColor(d.data.pct))
    .attr("rx", 3)
    .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);

  // Texto adaptativo: escala el tamaño de fuente con el ancho de la celda
  cells.each(function(d) {
    const g  = d3.select(this);
    const cw = d.x1 - d.x0;
    const ch = d.y1 - d.y0;
    const cx = (d.x0 + d.x1) / 2;
    const cy = (d.y0 + d.y1) / 2;

    if (cw < 28 || ch < 16) return;  // celda demasiado pequeña

    const fs   = Math.min(14, Math.max(7, cw / 5.5));    // fuente del ticker
    const fsp  = Math.min(11, Math.max(6, cw / 7.5));    // fuente del porcentaje
    const gap  = fs * 0.75;                               // separación vertical
    const sign = d.data.pct >= 0 ? "▲" : "▼";            // flecha de dirección

    if (ch >= 30) {
      // Celda alta: ticker arriba, porcentaje abajo
      g.append("text").attr("class", "ticker-name")
        .attr("x", cx).attr("y", cy - gap * 0.55)
        .attr("font-size", fs + "px").text(d.data.name)
        .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
      g.append("text").attr("class", "ticker-pct")
        .attr("x", cx).attr("y", cy + gap * 0.65)
        .attr("font-size", fsp + "px")
        .text(`${sign} ${Math.abs(d.data.pct).toFixed(2)}%`)
        .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
    } else {
      // Celda baja: solo el ticker centrado
      g.append("text").attr("class", "ticker-name")
        .attr("x", cx).attr("y", cy)
        .attr("font-size", fs + "px").text(d.data.name)
        .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
    }
  });
}


// ════════════════════════════════════════════════════════════════════════════════
// TOOLTIP — mousemove / mouseleave
// ════════════════════════════════════════════════════════════════════════════════
function onMouseMove(event, d) {
  if (d.data.pct == null) return;  // ignorar nodos de sector

  const pctCls = d.data.pct >= 0 ? "pos" : "neg";
  const sign   = d.data.pct >= 0 ? "+"  : "";
  const v      = d.value;
  const mcap   = v >= 1e12 ? `$${(v/1e12).toFixed(2)} T`
               : v >= 1e9  ? `$${(v/1e9).toFixed(2)} B`
               :              `$${(v/1e6).toFixed(0)} M`;

  tooltip.innerHTML = `
    <div class="tip-ticker">${d.data.name}</div>
    <div class="tip-name">${d.data.fullName}</div>
    <div class="tip-sep"></div>
    <div class="tip-row"><span>Industria</span>  <span class="tip-val">${d.parent.data.name}</span></div>
    <div class="tip-row"><span>Precio</span>     <span class="tip-val">$${Number(d.data.price).toLocaleString("es-CL",{minimumFractionDigits:2})}</span></div>
    <div class="tip-row"><span>Market Cap</span> <span class="tip-val">${mcap}</span></div>
    <div class="tip-row"><span>Variación</span>  <span class="tip-val ${pctCls}">${sign}${d.data.pct.toFixed(2)}%</span></div>
  `;

  // Posicionar evitando que el tooltip salga de la pantalla
  const pad = 16, tw = 220, th = 160;
  let tx = event.clientX + pad;
  let ty = event.clientY - pad;
  if (tx + tw > window.innerWidth)   tx = event.clientX - tw - pad;
  if (ty + th > window.innerHeight)  ty = event.clientY - th - pad;
  tooltip.style.left = tx + "px";
  tooltip.style.top  = ty + "px";
  tooltip.classList.add("visible");
}

function onMouseLeave() {
  tooltip.classList.remove("visible");
}


// ════════════════════════════════════════════════════════════════════════════════
// DESCARGA COMO PNG
//
// Usa html2canvas para capturar el documento completo (header + filtros +
// treemap SVG + leyenda) y lo descarga como archivo .png.
// El tooltip se oculta antes de capturar para no aparecer en la imagen.
// ════════════════════════════════════════════════════════════════════════════════
async function downloadPNG() {
  const btn = document.getElementById("btn-download");
  btn.disabled    = true;
  btn.textContent = "Generando…";

  // Ocultar tooltip antes de capturar
  tooltip.classList.remove("visible");

  try {
    const canvas = await html2canvas(document.body, {
      backgroundColor: "#0d1117",
      scale: Math.max(window.devicePixelRatio * 2, 4),
      useCORS: true,
      allowTaint: true,
      logging: false,
      // Ignorar el tooltip (fixed, oculto) para evitar artefactos
      ignoreElements: el => el.id === "tooltip",
    });

    // Nombre de archivo incluye el período activo
    const badge  = document.getElementById("period-badge").textContent
                     .replace("Var. ", "").replace(/\s*→\s*/g, "_").replace(/\s/g, "");
    const fname  = `IPSA_treemap_${badge || "export"}.png`;

    const link   = document.createElement("a");
    link.download = fname;
    link.href     = canvas.toDataURL("image/png");
    link.click();
  } catch (err) {
    alert("Error al generar PNG: " + err.message);
  } finally {
    btn.disabled    = false;
    btn.textContent = "⬇ PNG";
  }
}


// ════════════════════════════════════════════════════════════════════════════════
// INICIALIZACIÓN
// ════════════════════════════════════════════════════════════════════════════════
// Calcular la vista inicial con el rango por defecto
currentData = computeDataForRange(defaultStart, defaultEnd);
updateBadge(defaultStart, defaultEnd);
render(currentData);

// Redibujar cuando cambia el tamaño de la ventana (debounce de 80ms)
window.addEventListener("resize", () => {
  clearTimeout(window._rt);
  window._rt = setTimeout(() => render(currentData), 80);
});
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# 5. SERVIDOR HTTP LOCAL
#    Sirve el HTML en GET / y procesa solicitudes de datos frescos en GET /refresh.
#
#    La clase ThreadingHTTPServer permite manejar múltiples requests
#    simultáneos (ej: el navegador pide favicon mientras carga el HTML).
#    daemon_threads=True hace que los hilos mueran cuando el proceso principal termina.
# ─────────────────────────────────────────────────────────────────────────────

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Servidor HTTP multihilo para manejar requests concurrentes del navegador."""
    daemon_threads = True   # los hilos no bloquean el Ctrl+C


def make_handler(html_bytes: bytes):
    """
    Crea una clase handler con el HTML cacheado en su closure.
    Usamos una fábrica porque BaseHTTPRequestHandler no permite constructor custom
    sin sobreescribir __init__ (que tiene signatura fija).
    """

    class IPSAHandler(http.server.BaseHTTPRequestHandler):

        def do_GET(self):
            """Procesa todas las peticiones GET del navegador."""
            parsed = urllib.parse.urlparse(self.path)
            path   = parsed.path

            if path in ("/", "/index.html"):
                # Servir el HTML principal con los 13 meses de data embebida
                self._respond(200, "text/html; charset=utf-8", html_bytes)

            elif path == "/refresh":
                # Endpoint llamado por el botón "Actualizar" del HTML.
                # Descarga 13 meses completos de precios frescos desde Yahoo Finance
                # y retorna el árbol D3 completo como JSON.
                # El navegador reemplaza rawData.children con este resultado,
                # actualizando el caché local para que el filtro local use data reciente.
                try:
                    print(f"  [/refresh] Descargando 13 meses frescos ...")
                    result = refresh_and_build_json()
                    self._respond(200, "application/json", result.encode())
                    print(f"  [/refresh] OK — {len(result)//1024} KB retornados")
                except Exception as exc:
                    print(f"  [/refresh] ERROR: {exc}")
                    err = json.dumps({"error": str(exc)})
                    self._respond(500, "application/json", err.encode())

            elif path == "/favicon.ico":
                # Suprimir el error 404 que el navegador genera automáticamente
                self._respond(204, "text/plain", b"")

            else:
                self._respond(404, "text/plain", b"Not found")

        def _respond(self, code: int, content_type: str, body: bytes):
            """Envía una respuesta HTTP completa."""
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # Permitir que el HTML llame a /refresh desde el mismo origen
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            # Suprimir los logs HTTP por defecto (muy verbosos)
            # Solo mostramos los errores (no-200) de forma personalizada
            pass

    return IPSAHandler


def find_free_port(start: int = 8765) -> int:
    """
    Busca un puerto TCP libre comenzando desde 'start'.
    Prueba hasta 50 puertos consecutivos antes de rendirse.
    """
    for port in range(start, start + 50):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return start   # fallback; puede fallar si start también está ocupado


# ─────────────────────────────────────────────────────────────────────────────
# 6. ARRANQUE DEL SERVIDOR Y APERTURA DEL NAVEGADOR
# ─────────────────────────────────────────────────────────────────────────────

# Reemplazar los placeholders en la plantilla HTML con los valores reales
HTML = HTML_TEMPLATE
HTML = HTML.replace("DATA_PLACEHOLDER",     data_json)      # árbol D3 con 13 meses
HTML = HTML.replace("DEFAULT_START",        default_start)  # inicio vista inicial
HTML = HTML.replace("DEFAULT_END",          default_end)    # término vista inicial
HTML = HTML.replace("DATE_MIN_PLACEHOLDER", date_min)       # mínimo del date picker
HTML = HTML.replace("DATE_MAX_PLACEHOLDER", date_max)       # máximo del date picker

html_bytes = HTML.encode("utf-8")

# Encontrar un puerto libre y levantar el servidor
port    = int(os.environ.get("PORT", find_free_port(8765)))
handler = make_handler(html_bytes)
server  = ThreadingHTTPServer(("0.0.0.0", port), handler)

url = f"http://localhost:{port}/"
print(f"  Servidor iniciado en {url}")
print(f"  Vista inicial: {default_start} → {default_end}  (última semana)")
print()
print("  Controles en el navegador:")
print("    · [Filtrar]            — recalcular con datos locales (instantáneo)")
print("    · [↓ Actualizar datos] — descargar 13 meses frescos de Yahoo Finance")
print("    · [⬇ PNG]              — exportar la vista actual como imagen PNG")
print()
print("  Presione Ctrl+C para detener el servidor.\n")

# Abrir el navegador solo cuando corre localmente (no en servidor cloud)
if not os.environ.get("PORT"):
    threading.Timer(0.5, webbrowser.open, args=(url,)).start()

# Ejecutar el servidor indefinidamente hasta que el usuario presione Ctrl+C
try:
    server.serve_forever()
except KeyboardInterrupt:
    print("\n  Servidor detenido.")
    server.server_close()
