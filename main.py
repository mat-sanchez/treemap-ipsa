"""
Treemap IPSA — Capitalización de Mercado por Industria
Servidor HTTP local con actualizaciones en vivo desde el navegador.

Flujo completo:
  1. Al iniciar, descarga 13 meses de precios diarios del IPSA desde Yahoo Finance.
  2. Levanta un servidor HTTP local (autodetecta un puerto libre desde el 8765).
  3. Abre el navegador apuntando a http://localhost:PORT/.
  4. El HTML carga con los 13 meses de data embebida.
     - Cambiar las fechas en el panel de filtro → recalculo instantáneo (sin red).
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
import time

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
    # Escapar </ para que el JSON pueda embeberse en <script> sin riesgo de break-out
    return json.dumps(tree, ensure_ascii=False).replace("</", r"<\/")


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
data_json = json.dumps(d3_tree, ensure_ascii=False).replace("</", r"<\/")

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
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:ital,wght@0,300;0,400;0,500;1,300&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"
        integrity="sha384-CjloA8y00+1SDAUkjs099PVfnY2KmDC2BZnws9kh8D/lX1s46w6EPhpXdqMfjK6i"
        crossorigin="anonymous"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"
        integrity="sha384-ZZ1pncU3bQe8y31yfZdMFdSpttDoPmOZg2wguVK9almUodir1PghgT0eY7Mrty8H"
        crossorigin="anonymous"></script>
<style>
  :root {
    --bg:      #080c12;
    --surface: #0d1220;
    --card:    #111827;
    --border:  rgba(255,255,255,0.06);
    --border2: rgba(255,255,255,0.11);
    --accent:  #00d4aa;
    --blue:    #4f8dff;
    --text:    #e8edf5;
    --muted:   #5a6a82;
    --dim:     #3a4a5e;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Syne', sans-serif;
    font-weight: 400;
    display: flex;
    flex-direction: column;
    height: 100dvh;
    overflow: hidden;
  }

  /* Ambient glow blobs */
  body::before {
    content: '';
    position: fixed;
    top: -180px; left: -180px;
    width: 560px; height: 560px;
    background: radial-gradient(circle, rgba(0,212,170,0.055) 0%, transparent 65%);
    pointer-events: none;
    z-index: 0;
  }
  body::after {
    content: '';
    position: fixed;
    bottom: -180px; right: -120px;
    width: 500px; height: 500px;
    background: radial-gradient(circle, rgba(79,141,255,0.055) 0%, transparent 65%);
    pointer-events: none;
    z-index: 0;
  }

  #header, #date-filter, #progress-bar-wrap, #chart-container, #legend {
    position: relative;
    z-index: 1;
  }

  /* ── Header ── */
  #header {
    padding: 12px 24px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
    background: rgba(8,12,18,0.88);
    backdrop-filter: blur(24px);
  }

  .logo-text h1 {
    font-family: 'Syne', sans-serif;
    font-size: 22px;
    font-weight: 800;
    letter-spacing: -0.01em;
    color: var(--text);
    line-height: 1;
  }
  .logo-text .subtitle {
    font-family: 'DM Mono', monospace;
    font-size: 9px;
    letter-spacing: 0.14em;
    color: var(--muted);
    font-weight: 300;
    text-transform: uppercase;
    margin-top: 2px;
  }

  /* Toggle nominal / ajustado */
  .adj-toggle-wrap {
    display: flex;
    align-items: center;
    gap: 7px;
    cursor: pointer;
    user-select: none;
    margin-left: auto;
  }
  .adj-toggle-lbl {
    font-family: 'DM Mono', monospace;
    font-size: 9px;
    letter-spacing: 0.08em;
    color: var(--dim);
    white-space: nowrap;
    transition: color 0.2s;
    text-transform: uppercase;
  }
  .adj-toggle-lbl.on { color: var(--text); }

  #adj-toggle {
    position: relative;
    width: 40px; height: 22px;
    background: var(--card);
    border: 1px solid var(--border2);
    border-radius: 999px;
    flex-shrink: 0;
    transition: border-color 0.25s;
  }
  #adj-toggle:hover  { border-color: var(--muted); }
  #adj-toggle.active { border-color: var(--accent); }

  .toggle-knob {
    position: absolute;
    top: 3px; left: 3px;
    width: 14px; height: 14px;
    border-radius: 50%;
    background: var(--muted);
    transition: transform 0.25s cubic-bezier(0.4,0,0.2,1), background 0.25s;
  }
  #adj-toggle.active .toggle-knob {
    transform: translateX(18px);
    background: var(--accent);
  }

  /* ── Filter bar ── */
  #date-filter {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 8px 24px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
    flex-wrap: wrap;
  }

  /* Quick period preset buttons */
  .period-group {
    display: flex;
    gap: 2px;
    background: var(--card);
    padding: 3px;
    border-radius: 8px;
    border: 1px solid var(--border);
  }
  .period-btn {
    padding: 6px 12px;
    border: none;
    border-radius: 6px;
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.18s ease;
    background: transparent;
    color: var(--muted);
    letter-spacing: 0.04em;
  }
  .period-btn:hover  { color: var(--text); background: rgba(255,255,255,0.06); }
  .period-btn.active { background: linear-gradient(135deg, var(--accent), var(--blue)); color: #000; }

  .filter-divider {
    width: 1px; height: 20px;
    background: var(--border2);
    flex-shrink: 0;
  }

  .date-chip {
    background: var(--card);
    border: 1px solid var(--border2);
    border-radius: 6px;
    color: var(--text);
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    font-weight: 400;
    padding: 6px 10px;
    cursor: pointer;
    transition: border-color 0.15s;
    outline: none;
    color-scheme: dark;
  }
  .date-chip:hover { border-color: var(--muted); }
  .date-chip:focus { border-color: var(--accent); }

  .filter-arrow {
    font-family: 'DM Mono', monospace;
    color: var(--dim);
    font-size: 12px;
    user-select: none;
  }

  #btn-update {
    background: rgba(0,212,170,0.08);
    border: 1px solid rgba(0,212,170,0.28);
    border-radius: 6px;
    color: var(--accent);
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.08em;
    padding: 6px 14px;
    cursor: pointer;
    text-transform: uppercase;
    transition: all 0.15s;
    white-space: nowrap;
  }
  #btn-update:hover    { background: rgba(0,212,170,0.16); border-color: var(--accent); }
  #btn-update:active   { background: rgba(0,212,170,0.04); }
  #btn-update:disabled { opacity: 0.35; cursor: not-allowed; }

  #btn-download {
    background: transparent;
    border: 1px solid var(--border2);
    border-radius: 6px;
    color: var(--muted);
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.08em;
    padding: 6px 14px;
    cursor: pointer;
    text-transform: uppercase;
    transition: all 0.15s;
    white-space: nowrap;
  }
  #btn-download:hover    { border-color: var(--muted); color: var(--text); }
  #btn-download:disabled { opacity: 0.35; cursor: not-allowed; }

  #filter-status {
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    letter-spacing: 0.04em;
    color: var(--muted);
    transition: color 0.25s;
    min-width: 80px;
  }
  #filter-status.loading { color: var(--blue); }
  #filter-status.ok      { color: var(--accent); }
  #filter-status.error   { color: #ff4d6d; }

  /* Progress bar */
  #progress-bar-wrap {
    height: 2px;
    background: var(--border);
    flex-shrink: 0;
    display: none;
    overflow: hidden;
  }
  #progress-bar-fill {
    height: 100%;
    width: 40%;
    background: linear-gradient(90deg, transparent, var(--accent), var(--blue), transparent);
    animation: progress-slide 1.6s infinite linear;
  }
  @keyframes progress-slide {
    0%   { margin-left: -40%; }
    100% { margin-left: 100%; }
  }

  /* ── Treemap ── */
  #chart-container { flex: 1; position: relative; overflow: hidden; min-height: 0; }
  svg { width: 100%; height: 100%; display: block; }

  .industry-label {
    font-family: 'DM Mono', monospace;
    font-size: 9px;
    font-weight: 400;
    fill: rgba(90,106,130,0.65);
    text-transform: uppercase;
    letter-spacing: 0.14em;
    pointer-events: none;
  }

  /* Etiquetas de celda: layout bottom-left, tres niveles (ticker / empresa / pct) */
  .cell-ticker {
    font-family: 'DM Mono', monospace;
    font-weight: 500;
    fill: rgba(255,255,255,0.95);
    pointer-events: none;
    text-anchor: start;
  }

  .cell-name {
    font-family: 'Syne', sans-serif;
    font-weight: 400;
    fill: rgba(255,255,255,0.55);
    pointer-events: none;
    text-anchor: start;
  }

  .cell-pct {
    font-family: 'DM Mono', monospace;
    font-weight: 500;
    fill: rgba(255,255,255,0.95);
    pointer-events: none;
    text-anchor: start;
  }

  /* Hover brightness on cells */
  .cell rect.cell-fill { transition: filter 0.18s ease; }
  .cell:hover rect.cell-fill { filter: brightness(1.3) saturate(1.1); }

  /* ── Tooltip ── */
  #tooltip {
    position: fixed;
    background: var(--card);
    border: 1px solid var(--border2);
    border-radius: 10px;
    padding: 14px 18px;
    pointer-events: none;
    opacity: 0;
    transition: opacity 0.1s ease;
    min-width: 200px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.72), 0 0 0 1px rgba(255,255,255,0.04);
    z-index: 999;
  }
  #tooltip.visible { opacity: 1; }

  .tip-ticker {
    font-family: 'Syne', sans-serif;
    font-size: 20px;
    font-weight: 700;
    letter-spacing: 0.01em;
    color: var(--text);
    margin-bottom: 2px;
    line-height: 1.1;
  }
  .tip-name {
    font-family: 'DM Mono', monospace;
    font-size: 9px;
    letter-spacing: 0.08em;
    color: var(--muted);
    margin-bottom: 10px;
    text-transform: uppercase;
  }
  .tip-sep { height: 1px; background: var(--border); margin-bottom: 8px; }
  .tip-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 20px;
    margin-bottom: 5px;
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    letter-spacing: 0.04em;
    color: var(--muted);
    text-transform: uppercase;
  }
  .tip-val { color: var(--text); font-weight: 500; font-size: 11px; }
  .pos     { color: #00c897 !important; }
  .neg     { color: #ff4d6d !important; }

  /* ── Footer / Legend ── */
  #legend {
    padding: 7px 24px 8px;
    border-top: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 14px;
    flex-shrink: 0;
    background: var(--surface);
  }

  #legend-label {
    font-family: 'DM Mono', monospace;
    font-size: 9px;
    letter-spacing: 0.15em;
    color: var(--muted);
    text-transform: uppercase;
    white-space: nowrap;
  }

  #legend-wrap { display: flex; flex-direction: column; gap: 3px; }

  #legend-blocks {
    display: flex;
    gap: 1px;
    border-radius: 3px;
    overflow: hidden;
    height: 9px;
    width: 196px;
  }
  .lg-block { flex: 1; }

  #legend-ticks {
    display: flex;
    justify-content: space-between;
    font-family: 'DM Mono', monospace;
    font-size: 9px;
    color: var(--muted);
    width: 196px;
  }

  /* Movers bar */
  #movers-bar {
    display: flex;
    align-items: center;
    gap: 6px;
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    flex-wrap: wrap;
  }
  #movers-up, #movers-down {
    display: flex;
    gap: 4px;
    align-items: center;
    flex-wrap: wrap;
  }
  .mover-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 10px;
    white-space: nowrap;
  }
  .mover-chip.up   { background: rgba(0,200,151,0.12); color: #00c897; border: 1px solid rgba(0,200,151,0.25); }
  .mover-chip.down { background: rgba(255,77,109,0.12); color: #ff4d6d; border: 1px solid rgba(255,77,109,0.25); }
  .mover-chip .m-ticker { font-weight: 500; }
  .mover-chip .m-pct    { opacity: 0.8; }

  #legend-right { margin-left: auto; display: inline-flex; align-items: center; gap: 10px; }

  #linkedin-link {
    font-family: 'DM Mono', monospace;
    font-size: 9px;
    letter-spacing: 0.06em;
    color: var(--dim);
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    transition: color 0.2s;
  }
  #linkedin-link:hover { color: var(--blue); }

  /* Desktop: date-row es invisible — sus hijos participan directamente en el flex del padre */
  .date-row { display: contents; }

  /* Mobile */
  @media (max-width: 640px) {
    body { height: auto; overflow-y: auto; min-height: 100dvh; }
    #chart-container { height: auto; flex: none; overflow: visible; }

    #header { padding: 10px 14px; flex-wrap: wrap; gap: 8px; }
    .logo-text .subtitle { display: none; }
    .adj-toggle-wrap { margin-left: auto; order: 2; }
    .adj-toggle-lbl  { font-size: 8px; letter-spacing: 0.03em; }

    /* Barra de filtros: fila 1 = presets de período, fila 2 = fechas + acción */
    #date-filter {
      padding: 8px 14px;
      gap: 6px;
      flex-direction: column;
      align-items: stretch;
      flex-wrap: nowrap;
    }
    .filter-divider { display: none; }
    .period-group   { width: 100%; }
    .period-btn     { flex: 1; text-align: center; padding: 7px 4px; font-size: 10px; }
    .date-row       { display: flex; align-items: center; gap: 6px; }
    .date-chip      { flex: 1; min-width: 0; font-size: 10px; padding: 6px 7px; }
    #btn-update     { flex-shrink: 0; font-size: 9px; padding: 6px 10px; white-space: nowrap; }
    #btn-download   { display: none; }
    #filter-status  { display: none; }

    /* Tooltip acotado al ancho de pantalla */
    #tooltip { min-width: 0; width: 210px; padding: 12px 14px; }

    #legend { padding: 7px 14px; flex-wrap: wrap; gap: 8px; }
    #movers-bar   { display: none; }
    #legend-right { margin-left: 0; width: 100%; }
  }
</style>
  <!-- Cloudflare Web Analytics -->
  <script defer src='https://static.cloudflareinsights.com/beacon.min.js' data-cf-beacon='{"token": "1e000c43ec6c45cdb239a572900c9658"}'></script>
  <!-- End Cloudflare Web Analytics -->
</head>
<body>

<!-- ── Header ── -->
<div id="header">
  <div class="logo-text">
    <h1>IPSA</h1>
    <div class="subtitle">Bolsa de Santiago · Área = Market Cap</div>
  </div>

  <!-- Toggle nominal / ajustado por dividendos -->
  <div class="adj-toggle-wrap" onclick="toggleAdjusted()" title="Cambiar entre precios nominales y ajustados por dividendos">
    <span class="adj-toggle-lbl on" id="lbl-nominal">Nominal</span>
    <div id="adj-toggle"><div class="toggle-knob"></div></div>
    <span class="adj-toggle-lbl" id="lbl-adj">Ajustado por Dividendos</span>
  </div>
</div>

<!-- ── Panel de filtro de fechas ── -->
<!--
  Las fechas se aplican automáticamente al cambiar (sin botón "Filtrar").
  El botón "Actualizar" descarga 13 meses frescos desde Yahoo Finance.
-->
<div id="date-filter">
  <!-- Presets de período rápido -->
  <div class="period-group" id="period-btns">
    <button class="period-btn" data-days="0">HOY</button>
    <button class="period-btn active" data-days="7">7D</button>
    <button class="period-btn" data-days="30">1M</button>
    <button class="period-btn" data-days="90">3M</button>
    <button class="period-btn" data-days="180">6M</button>
    <button class="period-btn" data-days="365">1A</button>
  </div>

  <div class="filter-divider"></div>

  <div class="date-row">
    <input type="date" id="date-start" class="date-chip"
           value="DEFAULT_START"
           min="DATE_MIN_PLACEHOLDER"
           max="DATE_MAX_PLACEHOLDER"
           title="Fecha de inicio">

    <span class="filter-arrow">→</span>

    <input type="date" id="date-end" class="date-chip"
           value="DEFAULT_END"
           min="DATE_MIN_PLACEHOLDER"
           max="DATE_MAX_PLACEHOLDER"
           title="Fecha de término">

    <button id="btn-update" onclick="freshUpdate()">↓ Actualizar datos</button>
    <span id="filter-status"></span>
    <button id="btn-download" onclick="downloadPNG()" title="Descargar imagen PNG">⬇ PNG</button>
  </div>
</div>

<!-- ── Barra de progreso (visible solo durante "Actualizar") ── -->
<div id="progress-bar-wrap">
  <div id="progress-bar-fill"></div>
</div>

<!-- ── Treemap SVG ── -->
<div id="chart-container">
  <svg id="treemap"></svg>
</div>

<div id="tooltip"></div>

<!-- ── Leyenda + movers bar ── -->
<div id="legend">
  <span id="legend-label">Variación %</span>
  <div id="legend-wrap">
    <div id="legend-blocks">
      <div class="lg-block" style="background:#ff4d6d"></div>
      <div class="lg-block" style="background:#d93050"></div>
      <div class="lg-block" style="background:#a02040"></div>
      <div class="lg-block" style="background:#2a3548"></div>
      <div class="lg-block" style="background:#007a5a"></div>
      <div class="lg-block" style="background:#00a87e"></div>
      <div class="lg-block" style="background:#00c897"></div>
    </div>
    <div id="legend-ticks">
      <span>-4%</span><span>-2%</span><span>0</span><span>+2%</span><span>+4%</span>
    </div>
  </div>

  <div id="movers-bar">
    <span id="movers-up"></span>
    <span id="movers-down"></span>
  </div>

  <span id="legend-right">
    <a id="linkedin-link" href="https://www.linkedin.com/in/mat%C3%ADas-alejandro-s%C3%A1nchez-ruiz/" target="_blank" rel="noopener">
      <svg xmlns="http://www.w3.org/2000/svg" width="11" height="11" viewBox="0 0 24 24" fill="currentColor">
        <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 0 1-2.063-2.065 2.064 2.064 0 1 1 2.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/>
      </svg>
      Matías Sánchez Ruiz
    </a>
  </span>
</div>

<script>
// ════════════════════════════════════════════════════════════════════════════════
// DATOS EMBEBIDOS (generados por Python al arrancar el servidor)
// ════════════════════════════════════════════════════════════════════════════════
const rawData = DATA_PLACEHOLDER;

const defaultStart = "DEFAULT_START";
const defaultEnd   = "DEFAULT_END";

let currentData = null;

// useAdjusted: si true usa adjPrices en lugar de prices para calcular pct
let useAdjusted = false;

// Celda activa en touch (para toggle: primer tap muestra, segundo tap oculta)
let _touchCell = null;

// Rango de fechas activo; usado por downloadPNG para el nombre del archivo
let _activeDateRange = { start: defaultStart, end: defaultEnd };


// ════════════════════════════════════════════════════════════════════════════════
// ESCALA DE COLOR POR PASOS (discreta)
// Replica los colores del diseño de referencia con 7 niveles saturados.
// ════════════════════════════════════════════════════════════════════════════════
function discreteColor(pct) {
  if (pct >= 4)   return "#00c897";
  if (pct >= 2)   return "#00a87e";
  if (pct >= 0.5) return "#007a5a";
  if (pct > -0.5) return "#2a3548";
  if (pct > -2)   return "#a02040";
  if (pct > -4)   return "#d93050";
  return "#ff4d6d";
}


// ════════════════════════════════════════════════════════════════════════════════
// BOTONES DE PERÍODO RÁPIDO
// data-days="0": HOY → start = end = today → cambio diario real (base = cierre anterior)
// data-days="N": start = today − N días, end = today
// ════════════════════════════════════════════════════════════════════════════════
function formatDateISO(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

document.getElementById("period-btns").addEventListener("click", e => {
  const btn = e.target.closest(".period-btn");
  if (!btn) return;
  document.querySelectorAll(".period-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  const days = parseInt(btn.dataset.days);
  const endDate   = new Date();
  const startDate = new Date();
  if (days > 0) startDate.setDate(endDate.getDate() - days);
  document.getElementById("date-end").value   = formatDateISO(endDate);
  document.getElementById("date-start").value = formatDateISO(startDate);
  applyFilter();
});

// Auto-filter al cambiar manualmente los date pickers
["date-start", "date-end"].forEach(id => {
  document.getElementById(id).addEventListener("change", () => {
    document.querySelectorAll(".period-btn").forEach(b => b.classList.remove("active"));
    applyFilter();
  });
});


// ════════════════════════════════════════════════════════════════════════════════
// MOVERS BAR — top 3 gainers y losers del período actual
// ════════════════════════════════════════════════════════════════════════════════
function updateMovers(data) {
  const leaves = [];
  data.children.forEach(s => s.children.forEach(t => leaves.push(t)));
  if (!leaves.length) return;

  leaves.sort((a, b) => b.pct - a.pct);
  const topN    = Math.min(3, Math.floor(leaves.length / 2));
  const gainers = leaves.slice(0, topN);
  const losers  = leaves.slice(-topN).reverse();

  function buildChip(t, cls) {
    const chip = document.createElement("span");
    chip.className = `mover-chip ${cls}`;
    const ticker = document.createElement("span");
    ticker.className = "m-ticker";
    ticker.textContent = t.name;
    const pct = document.createElement("span");
    pct.className = "m-pct";
    pct.textContent = cls === "up"
      ? ` +${t.pct.toFixed(2)}%`
      : ` ${t.pct.toFixed(2)}%`;
    chip.appendChild(ticker);
    chip.appendChild(pct);
    return chip;
  }

  document.getElementById("movers-up").replaceChildren(
    ...gainers.map(t => buildChip(t, "up"))
  );
  document.getElementById("movers-down").replaceChildren(
    ...losers.map(t => buildChip(t, "down"))
  );
}


// ════════════════════════════════════════════════════════════════════════════════
// RECALCULAR DATOS PARA UN RANGO DE FECHAS (operación local, sin red)
// ════════════════════════════════════════════════════════════════════════════════
function computeDataForRange(startStr, endStr) {
  const newSectors = rawData.children.map(sector => {
    const newTickers = sector.children.map(ticker => {
      const priceData = useAdjusted ? ticker.adjPrices : ticker.prices;
      const dates = Object.keys(priceData).sort();

      const beforeStart = dates.filter(d => d < startStr);
      const toEnd       = dates.filter(d => d <= endStr);

      if (!beforeStart.length || !toEnd.length) return null;

      const dIni = beforeStart[beforeStart.length - 1];
      const dFin = toEnd[toEnd.length - 1];
      const pIni = priceData[dIni];
      const pFin = priceData[dFin];

      const pct = +((pFin / pIni - 1) * 100).toFixed(2);
      // Market cap siempre con precio nominal para que el área refleje el valor de mercado real
      const nominalFin = ticker.prices[dFin] ?? pFin;
      const mcap = ticker.shares > 0 ? ticker.shares * nominalFin : nominalFin * 1e6;

      return { ...ticker, pct, price: nominalFin, value: mcap };
    }).filter(Boolean);

    return { name: sector.name, children: newTickers };
  }).filter(s => s.children.length > 0);

  return { name: "IPSA", children: newSectors };
}


// ════════════════════════════════════════════════════════════════════════════════
// UTILIDADES DE UI
// ════════════════════════════════════════════════════════════════════════════════
function showProgress() { document.getElementById("progress-bar-wrap").style.display = "block"; }
function hideProgress() { document.getElementById("progress-bar-wrap").style.display = "none"; }

function setStatus(type, msg) {
  const el = document.getElementById("filter-status");
  el.textContent = msg;
  el.className   = type;
}

function updateBadge(startStr, endStr) {
  _activeDateRange = { start: startStr, end: endStr };
}

function validateDates() {
  const startVal = document.getElementById("date-start").value;
  const endVal   = document.getElementById("date-end").value;
  if (!startVal || !endVal)  { setStatus("error", "⚠ Fechas inválidas");    return null; }
  if (startVal > endVal)     { setStatus("error", "⚠ Rango inválido");       return null; }
  return { startVal, endVal };
}

function countLeaves(tree) {
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
  const dates = validateDates();
  if (!dates) return;
  currentData = computeDataForRange(dates.startVal, dates.endVal);
  if (countLeaves(currentData) > 0) render(currentData);
}


// ════════════════════════════════════════════════════════════════════════════════
// APLICAR FILTRO LOCAL — sin botón; llamado por presets y por cambio de fecha
// ════════════════════════════════════════════════════════════════════════════════
function applyFilter() {
  const dates = validateDates();
  if (!dates) return;
  const { startVal, endVal } = dates;

  currentData = computeDataForRange(startVal, endVal);

  if (countLeaves(currentData) === 0) {
    setStatus("error", "⚠ Sin datos en ese rango");
    return;
  }

  updateBadge(startVal, endVal);
  setStatus("ok", `✓ ${countLeaves(currentData)} tickers`);
  render(currentData);
}


// ════════════════════════════════════════════════════════════════════════════════
// ACTUALIZAR CON DATOS FRESCOS — descarga 13 meses nuevos desde Yahoo Finance
// ════════════════════════════════════════════════════════════════════════════════
async function freshUpdate() {
  const dates = validateDates();
  if (!dates) return;
  const { startVal, endVal } = dates;

  const btn = document.getElementById("btn-update");
  btn.disabled    = true;
  btn.textContent = "Descargando...";
  setStatus("loading", "⟳ Descargando...");
  showProgress();

  try {
    const response = await fetch("/refresh");

    if (!response.ok) {
      const err = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
      throw new Error(err.error || `HTTP ${response.status}`);
    }

    const freshTree = await response.json();
    if (freshTree.error) throw new Error(freshTree.error);

    rawData.children = freshTree.children;

    currentData = computeDataForRange(startVal, endVal);

    if (countLeaves(currentData) === 0) {
      setStatus("error", "⚠ Sin datos");
      return;
    }

    updateBadge(startVal, endVal);
    setStatus("ok", `✓ ${countLeaves(currentData)} tickers · frescos`);
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
// Escala de color: discreteColor() — 7 pasos saturados (mismo esquema del diseño ref.)
// Sombra inferior: gradiente SVG objectBoundingBox → transparent→rgba(0,0,0,0.3)
//   replica el efecto ::before { linear-gradient(to bottom, transparent 40%, rgba(0,0,0,0.3)) }
// En móvil (≤640px) se usa renderMobile(): sectores apilados verticalmente.
// En escritorio se usa renderDesktop(): treemap 2D clásico.
// ════════════════════════════════════════════════════════════════════════════════
const svg       = d3.select("#treemap");
const container = document.getElementById("chart-container");
const tooltip   = document.getElementById("tooltip");

function isMobile() { return window.innerWidth <= 640; }

function render(data) {
  if (isMobile()) { renderMobile(data); } else { renderDesktop(data); }
}

function renderDesktop(data) {
  svg.style("height", null).attr("height", null);
  svg.selectAll("*").remove();

  const W = container.clientWidth;
  const H = container.clientHeight;
  svg.attr("viewBox", `0 0 ${W} ${H}`);

  // Gradiente para sombra inferior en cada celda (objectBoundingBox = relativo a cada rect)
  const defs = svg.append("defs");
  const grad = defs.append("linearGradient")
    .attr("id", "cell-shadow")
    .attr("x1", "0").attr("y1", "0")
    .attr("x2", "0").attr("y2", "1");
  grad.append("stop").attr("offset", "0%")   .attr("stop-color", "#000").attr("stop-opacity", "0");
  grad.append("stop").attr("offset", "40%")  .attr("stop-color", "#000").attr("stop-opacity", "0");
  grad.append("stop").attr("offset", "100%") .attr("stop-color", "#000").attr("stop-opacity", "0.32");

  const root = d3.hierarchy(data)
    .sum(d => d.value || 0)
    .sort((a, b) => b.value - a.value);

  d3.treemap()
    .tile(d3.treemapSquarify.ratio(1.2))
    .size([W, H])
    .paddingOuter(5)
    .paddingTop(20)
    .paddingInner(2)
    (root);

  // ── Grupos de sector ────────────────────────────────────────────────────────
  const groups = svg.selectAll(".ig").data(root.children).join("g").attr("class", "ig");

  groups.append("rect")
    .attr("x", d => d.x0).attr("y", d => d.y0)
    .attr("width",  d => Math.max(0, d.x1 - d.x0))
    .attr("height", d => Math.max(0, d.y1 - d.y0))
    .attr("fill", "#0c1628").attr("rx", 6);

  groups.append("text")
    .attr("class", "industry-label")
    .attr("x", d => d.x0 + 8).attr("y", d => d.y0 + 13)
    .text(d => {
      const maxChars = Math.floor((d.x1 - d.x0) / 6.5);
      return d.data.name.length > maxChars
        ? d.data.name.substring(0, maxChars - 1) + "…"
        : d.data.name;
    });

  // ── Celdas individuales (tickers) ──────────────────────────────────────────
  const cells = svg.selectAll(".cell").data(root.leaves()).join("g")
    .attr("class", "cell").style("cursor", "pointer");

  // 1. Fondo coloreado por variación %
  cells.append("rect")
    .attr("class", "cell-fill")
    .attr("x", d => d.x0 + 1).attr("y", d => d.y0 + 1)
    .attr("width",  d => Math.max(0, d.x1 - d.x0 - 2))
    .attr("height", d => Math.max(0, d.y1 - d.y0 - 2))
    .attr("fill", d => discreteColor(d.data.pct))
    .attr("rx", 5)
    .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave)
    .on("touchstart", onTouchStart);

  // 2. Sombra inferior (gradient overlay, no recibe eventos de mouse)
  cells.append("rect")
    .attr("x", d => d.x0 + 1).attr("y", d => d.y0 + 1)
    .attr("width",  d => Math.max(0, d.x1 - d.x0 - 2))
    .attr("height", d => Math.max(0, d.y1 - d.y0 - 2))
    .attr("fill", "url(#cell-shadow)")
    .attr("rx", 5)
    .attr("pointer-events", "none");

  // 3. Etiquetas ancladas al fondo-izquierda de cada celda
  // Layout de arriba a abajo: ticker → nombre empresa → pct
  cells.each(function(d) {
    const g  = d3.select(this);
    const cw = d.x1 - d.x0;
    const ch = d.y1 - d.y0;

    if (cw < 28 || ch < 18) return;

    const xl       = d.x0 + 10;
    const fsTicker = Math.min(13, Math.max(7, cw / 5.5));
    const fsPct    = Math.min(14, Math.max(7, cw / 5.5));
    const fsName   = 9;
    const pctSign  = d.data.pct >= 0 ? "+" : "";
    const pctText  = `${pctSign}${d.data.pct.toFixed(2)}%`;

    const pctY    = d.y1 - 10;
    const nameY   = pctY - fsPct - 4;
    const tickerY = nameY - fsName - 3;

    if (ch >= 55 && tickerY > d.y0 + 4) {
      const maxChars = Math.max(0, Math.floor((cw - 20) / 5.5));
      const fullName = d.data.fullName || "";
      const nameTrunc = fullName.length > maxChars
        ? fullName.substring(0, Math.max(0, maxChars - 1)) + "…"
        : fullName;

      g.append("text").attr("class", "cell-ticker")
        .attr("x", xl).attr("y", tickerY)
        .attr("font-size", fsTicker + "px").text(d.data.name)
        .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
      g.append("text").attr("class", "cell-name")
        .attr("x", xl).attr("y", nameY)
        .attr("font-size", fsName + "px").text(nameTrunc)
        .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
      g.append("text").attr("class", "cell-pct")
        .attr("x", xl).attr("y", pctY)
        .attr("font-size", fsPct + "px").text(pctText)
        .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);

    } else if (ch >= 36) {
      const ty = pctY - fsPct - 5;
      g.append("text").attr("class", "cell-ticker")
        .attr("x", xl).attr("y", ty > d.y0 + 4 ? ty : pctY - fsTicker - 3)
        .attr("font-size", fsTicker + "px").text(d.data.name)
        .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
      g.append("text").attr("class", "cell-pct")
        .attr("x", xl).attr("y", pctY)
        .attr("font-size", fsPct + "px").text(pctText)
        .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);

    } else {
      g.append("text").attr("class", "cell-ticker")
        .attr("x", xl).attr("y", pctY)
        .attr("font-size", fsTicker + "px").text(d.data.name)
        .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
    }
  });

  updateMovers(data);
}


// ════════════════════════════════════════════════════════════════════════════════
// RENDERIZADO MÓVIL — sectores apilados verticalmente, scroll nativo
// Cada sector recibe altura proporcional a su market cap (mínimo 90px).
// Dentro de cada sector se aplica d3.treemap() igual que en escritorio.
// ════════════════════════════════════════════════════════════════════════════════
function renderMobile(data) {
  svg.selectAll("*").remove();

  const W          = container.clientWidth || window.innerWidth;
  const sectorGap  = 4;
  const headerH    = 18;
  const minSectorH = 90;

  const totalValue = data.children.reduce((sum, s) =>
    sum + s.children.reduce((a, t) => a + (t.value || 0), 0), 0);

  // Altura proporcional al market cap de cada sector, con mínimo garantizado
  const targetTotal = Math.max(data.children.length * 130, 1400);
  const sectorHeights = data.children.map(s => {
    const sv = s.children.reduce((a, t) => a + (t.value || 0), 0);
    return Math.max(minSectorH, (sv / totalValue) * targetTotal);
  });
  const totalH = sectorHeights.reduce((sum, h) => sum + h, 0)
               + (data.children.length + 1) * sectorGap;

  svg.attr("viewBox", `0 0 ${W} ${totalH}`)
     .attr("height", totalH)
     .style("height", totalH + "px");

  // Gradiente de sombra inferior (igual que en escritorio)
  const defs = svg.append("defs");
  const grad = defs.append("linearGradient")
    .attr("id", "cell-shadow")
    .attr("x1", "0").attr("y1", "0")
    .attr("x2", "0").attr("y2", "1");
  grad.append("stop").attr("offset", "0%")   .attr("stop-color", "#000").attr("stop-opacity", "0");
  grad.append("stop").attr("offset", "40%")  .attr("stop-color", "#000").attr("stop-opacity", "0");
  grad.append("stop").attr("offset", "100%") .attr("stop-color", "#000").attr("stop-opacity", "0.32");

  let yOffset = sectorGap;

  data.children.forEach((sector, si) => {
    const sH = sectorHeights[si];
    const sX = sectorGap;
    const sW = W - sectorGap * 2;

    // Fondo del sector
    svg.append("rect")
      .attr("x", sX).attr("y", yOffset)
      .attr("width", sW).attr("height", sH)
      .attr("fill", "#0c1628").attr("rx", 6);

    // Etiqueta del sector
    svg.append("text")
      .attr("class", "industry-label")
      .attr("x", sX + 8).attr("y", yOffset + 13)
      .text(sector.name);

    // Área interior para los tickers
    const innerX = sX + 2;
    const innerY = yOffset + headerH + 2;
    const innerW = sW - 4;
    const innerH = sH - headerH - 4;

    // Treemap D3 para los tickers de este sector
    const sectorRoot = d3.hierarchy({ name: sector.name, children: sector.children })
      .sum(d => d.value || 0)
      .sort((a, b) => b.value - a.value);

    d3.treemap()
      .tile(d3.treemapSquarify.ratio(1.2))
      .size([innerW, innerH])
      .paddingInner(2)
      (sectorRoot);

    // Grupo desplazado al origen del área interior del sector
    const sectorGroup = svg.append("g")
      .attr("transform", `translate(${innerX}, ${innerY})`);

    const cells = sectorGroup.selectAll(".cell")
      .data(sectorRoot.leaves())
      .join("g")
      .attr("class", "cell")
      .style("cursor", "pointer");

    // Fondo de cada celda coloreado por variación %
    cells.append("rect")
      .attr("class", "cell-fill")
      .attr("x", d => d.x0 + 1).attr("y", d => d.y0 + 1)
      .attr("width",  d => Math.max(0, d.x1 - d.x0 - 2))
      .attr("height", d => Math.max(0, d.y1 - d.y0 - 2))
      .attr("fill", d => discreteColor(d.data.pct))
      .attr("rx", 5)
      .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave)
      .on("touchstart", onTouchStart);

    // Sombra inferior (gradient overlay, sin eventos de puntero)
    cells.append("rect")
      .attr("x", d => d.x0 + 1).attr("y", d => d.y0 + 1)
      .attr("width",  d => Math.max(0, d.x1 - d.x0 - 2))
      .attr("height", d => Math.max(0, d.y1 - d.y0 - 2))
      .attr("fill", "url(#cell-shadow)")
      .attr("rx", 5)
      .attr("pointer-events", "none");

    // Etiquetas ancladas al fondo-izquierda de cada celda (mismo criterio que escritorio)
    cells.each(function(d) {
      const g  = d3.select(this);
      const cw = d.x1 - d.x0;
      const ch = d.y1 - d.y0;

      if (cw < 28 || ch < 18) return;

      const xl       = d.x0 + 10;
      const fsTicker = Math.min(13, Math.max(7, cw / 5.5));
      const fsPct    = Math.min(14, Math.max(7, cw / 5.5));
      const fsName   = 9;
      const pctSign  = d.data.pct >= 0 ? "+" : "";
      const pctText  = `${pctSign}${d.data.pct.toFixed(2)}%`;

      const pctY    = d.y1 - 10;
      const nameY   = pctY - fsPct - 4;
      const tickerY = nameY - fsName - 3;

      if (ch >= 55 && tickerY > d.y0 + 4) {
        const maxChars  = Math.max(0, Math.floor((cw - 20) / 5.5));
        const fullName  = d.data.fullName || "";
        const nameTrunc = fullName.length > maxChars
          ? fullName.substring(0, Math.max(0, maxChars - 1)) + "…"
          : fullName;
        g.append("text").attr("class", "cell-ticker")
          .attr("x", xl).attr("y", tickerY)
          .attr("font-size", fsTicker + "px").text(d.data.name)
          .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
        g.append("text").attr("class", "cell-name")
          .attr("x", xl).attr("y", nameY)
          .attr("font-size", fsName + "px").text(nameTrunc)
          .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
        g.append("text").attr("class", "cell-pct")
          .attr("x", xl).attr("y", pctY)
          .attr("font-size", fsPct + "px").text(pctText)
          .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
      } else if (ch >= 36) {
        const ty = pctY - fsPct - 5;
        g.append("text").attr("class", "cell-ticker")
          .attr("x", xl).attr("y", ty > d.y0 + 4 ? ty : pctY - fsTicker - 3)
          .attr("font-size", fsTicker + "px").text(d.data.name)
          .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
        g.append("text").attr("class", "cell-pct")
          .attr("x", xl).attr("y", pctY)
          .attr("font-size", fsPct + "px").text(pctText)
          .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
      } else {
        g.append("text").attr("class", "cell-ticker")
          .attr("x", xl).attr("y", pctY)
          .attr("font-size", fsTicker + "px").text(d.data.name)
          .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
      }
    });

    yOffset += sH + sectorGap;
  });

  updateMovers(data);
}


// ════════════════════════════════════════════════════════════════════════════════
// TOOLTIP — XSS-safe: construido con DOM APIs, nunca innerHTML
// ════════════════════════════════════════════════════════════════════════════════
function onMouseMove(event, d) {
  if (d.data.pct == null) return;

  const pctCls = d.data.pct >= 0 ? "pos" : "neg";
  const sign   = d.data.pct >= 0 ? "+"  : "";
  const v      = d.value;
  const mcap   = v >= 1e12 ? `$${(v/1e12).toFixed(2)} T`
               : v >= 1e9  ? `$${(v/1e9).toFixed(2)} B`
               :              `$${(v/1e6).toFixed(0)} M`;

  function tipEl(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls)          e.className   = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function tipRow(label, value, cls) {
    const r = tipEl("div", "tip-row");
    r.appendChild(tipEl("span", null, label));
    r.appendChild(tipEl("span", cls ? `tip-val ${cls}` : "tip-val", value));
    return r;
  }
  tooltip.replaceChildren(
    tipEl("div", "tip-ticker", d.data.name),
    tipEl("div", "tip-name",   d.data.fullName),
    tipEl("div", "tip-sep"),
    tipRow("Industria",  d.parent.data.name),
    tipRow("Precio",     `$${Number(d.data.price).toLocaleString("es-CL",{minimumFractionDigits:2})}`),
    tipRow("Market Cap", mcap),
    tipRow("Variación",  `${sign}${d.data.pct.toFixed(2)}%`, pctCls),
  );

  const pad = 12, tw = 220, th = 170;
  let tx = event.clientX + pad;
  let ty = event.clientY - pad;
  tx = Math.max(pad, Math.min(tx, window.innerWidth  - tw - pad));
  ty = Math.max(pad, Math.min(ty, window.innerHeight - th - pad));
  tooltip.style.left = tx + "px";
  tooltip.style.top  = ty + "px";
  tooltip.classList.add("visible");
}

function onMouseLeave() {
  tooltip.classList.remove("visible");
}

function onTouchStart(event, d) {
  if (_touchCell === d) {
    onMouseLeave();
    _touchCell = null;
    return;
  }
  _touchCell = d;
  const t = event.touches[0];
  onMouseMove({ clientX: t.clientX, clientY: t.clientY }, d);
}


// ════════════════════════════════════════════════════════════════════════════════
// DESCARGA COMO PNG
// ════════════════════════════════════════════════════════════════════════════════
async function downloadPNG() {
  const btn = document.getElementById("btn-download");
  btn.disabled    = true;
  btn.textContent = "Generando…";
  tooltip.classList.remove("visible");

  try {
    const canvas = await html2canvas(document.body, {
      backgroundColor: "#080c12",
      scale: Math.max(window.devicePixelRatio * 2, 4),
      useCORS: true,
      logging: false,
      ignoreElements: el => el.id === "tooltip",
    });

    const { start, end } = _activeDateRange;
    const fname = `IPSA_treemap_${start}_${end || "export"}.png`;
    const link  = document.createElement("a");
    link.download = fname;
    link.href     = canvas.toDataURL("image/png");
    link.click();
  } catch (err) {
    console.error("PNG error:", err);
    alert("Error al generar PNG. Intenta nuevamente.");
  } finally {
    btn.disabled    = false;
    btn.textContent = "⬇ PNG";
  }
}


// ════════════════════════════════════════════════════════════════════════════════
// INICIALIZACIÓN
// ════════════════════════════════════════════════════════════════════════════════
currentData = computeDataForRange(defaultStart, defaultEnd);
updateBadge(defaultStart, defaultEnd);
render(currentData);

window.addEventListener("resize", () => {
  clearTimeout(window._rt);
  window._rt = setTimeout(() => render(currentData), 80);
});

// Ocultar tooltip al tocar fuera de una celda o al hacer scroll
document.addEventListener("touchstart", e => {
  if (!e.target.closest(".cell")) { onMouseLeave(); _touchCell = null; }
}, { passive: true });
window.addEventListener("scroll", () => { onMouseLeave(); _touchCell = null; }, { passive: true });
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
    # Estado de rate limiting compartido entre hilos via closure
    _state         = {"last_refresh": 0.0}
    _refresh_lock  = threading.Lock()
    _REFRESH_COOLDOWN = 60  # segundos mínimos entre actualizaciones

    class IPSAHandler(http.server.BaseHTTPRequestHandler):

        def do_GET(self):
            """Procesa todas las peticiones GET del navegador."""
            parsed = urllib.parse.urlparse(self.path)
            path   = parsed.path

            if path in ("/", "/index.html"):
                # Servir el HTML principal con los 13 meses de data embebida
                self._respond(200, "text/html; charset=utf-8", html_bytes)

            elif path == "/refresh":
                # Rate limiting: máximo una descarga cada _REFRESH_COOLDOWN segundos
                with _refresh_lock:
                    now  = time.time()
                    wait = _REFRESH_COOLDOWN - (now - _state["last_refresh"])
                    if wait > 0:
                        secs = int(wait) + 1
                        err  = json.dumps({"error": f"Demasiadas solicitudes. Espera {secs}s."})
                        self._respond(429, "application/json", err.encode())
                        return
                    _state["last_refresh"] = now

                try:
                    print(f"  [/refresh] Descargando 13 meses frescos ...")
                    result = refresh_and_build_json()
                    self._respond(200, "application/json", result.encode())
                    print(f"  [/refresh] OK — {len(result)//1024} KB retornados")
                except Exception as exc:
                    print(f"  [/refresh] ERROR: {exc}")
                    err = json.dumps({"error": "Error al actualizar los datos. Intenta nuevamente."})
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
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            if "text/html" in content_type:
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; "
                    "script-src 'self' https://cdnjs.cloudflare.com https://static.cloudflareinsights.com 'unsafe-inline'; "
                    "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
                    "font-src https://fonts.gstatic.com; "
                    "connect-src 'self' https://fonts.googleapis.com https://fonts.gstatic.com https://cloudflareinsights.com; "
                    "img-src 'self' data:; "
                    "object-src 'none'; "
                    "frame-ancestors 'none';"
                )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            # Suprimir los logs HTTP por defecto (muy verbosos)
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
_port_env = os.environ.get("PORT")
if _port_env is not None:
    try:
        port = int(_port_env)
    except ValueError:
        raise ValueError(f"PORT inválido: '{_port_env}'. Debe ser un número entero.")
    if not (1 <= port <= 65535):
        raise ValueError(f"PORT inválido: {port}. Debe estar entre 1 y 65535.")
else:
    port = find_free_port(8765)
handler     = make_handler(html_bytes)
_bind_host  = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
server      = ThreadingHTTPServer((_bind_host, port), handler)

url = f"http://localhost:{port}/"
print(f"  Servidor iniciado en {url}")
print(f"  Vista inicial: {default_start} → {default_end}  (última semana)")
print()
print("  Controles en el navegador:")
print("    · [HOY/7D/1M/...] — presets de período (aplica automáticamente)")
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
