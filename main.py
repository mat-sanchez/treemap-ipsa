"""
Treemap IPSA — Capitalización de Mercado por Industria
Servidor HTTP local con actualizaciones en vivo desde el navegador.

Flujo completo:
  1. Al iniciar, descarga 13 meses de precios diarios del IPSA desde Yahoo Finance.
  2. Lee los archivos estáticos (static/index.html, static/app.js, static/style.css).
  3. Construye el payload de inicialización (árbol D3 + fechas) y lo embebe en el HTML.
  4. Levanta un servidor HTTP local (autodetecta un puerto libre desde el 8765).
  5. Abre el navegador apuntando a http://localhost:PORT/.
     - Cambiar las fechas en el panel de filtro → recalculo instantáneo (sin red).
     - Clic en "Actualizar" → descarga 13 meses frescos desde Yahoo Finance y
                              reemplaza el caché completo del navegador.
     - Clic en "⬇ PNG"     → exporta la vista actual (header + filtros + treemap
                             + leyenda) como imagen PNG de alta resolución.
  6. El servidor corre indefinidamente; se detiene con Ctrl+C en la consola.
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

from datetime import timedelta, date
from data import IPSA, download_prices, build_hierarchy, refresh_and_build_json


# ─────────────────────────────────────────────────────────────────────────────
# 1. ARCHIVOS ESTÁTICOS
#    Leídos una sola vez al arrancar; servidos desde memoria en cada request.
# ─────────────────────────────────────────────────────────────────────────────
_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
_STATIC_DIR = os.path.join(_BASE_DIR, "static")

with open(os.path.join(_STATIC_DIR, "index.html"), encoding="utf-8") as _f:
    _HTML_TEMPLATE = _f.read()

_APP_JS_BYTES    = open(os.path.join(_STATIC_DIR, "app.js"),    encoding="utf-8").read().encode("utf-8")
_STYLE_CSS_BYTES = open(os.path.join(_STATIC_DIR, "style.css"), encoding="utf-8").read().encode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# 2. DESCARGA INICIAL — 13 MESES DE DATOS DIARIOS
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
date_min = hist_start
date_max = today.strftime("%Y-%m-%d")

# Construir árbol D3 con datos iniciales (vista de última semana)
print("  Construyendo jerarquía inicial ...")
d3_tree = build_hierarchy(close_full, adj_close_full, default_start, default_end)

total_init = sum(len(s["children"]) for s in d3_tree["children"])
print(f"  {total_init}/{len(IPSA)} tickers incluidos en el rango inicial\n")


# ─────────────────────────────────────────────────────────────────────────────
# 3. CONSTRUCCIÓN DEL HTML INICIAL
#    El payload de inicialización se embebe como <script type="application/json">
#    en el template. Solo un placeholder: INIT_PLACEHOLDER.
#    type="application/json" no es ejecutable → no requiere 'unsafe-inline' en CSP.
# ─────────────────────────────────────────────────────────────────────────────
_init_payload = json.dumps({
    "tree":         d3_tree,
    "defaultStart": default_start,
    "defaultEnd":   default_end,
    "dateMin":      date_min,
    "dateMax":      date_max,
}, ensure_ascii=False).replace("</", r"<\/")

html_bytes = _HTML_TEMPLATE.replace("INIT_PLACEHOLDER", _init_payload).encode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# 4. SERVIDOR HTTP LOCAL
#    ThreadingHTTPServer permite manejar múltiples requests simultáneos
#    (ej: el navegador pide app.js mientras carga el HTML).
#    daemon_threads=True hace que los hilos mueran cuando el proceso principal termina.
# ─────────────────────────────────────────────────────────────────────────────

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Servidor HTTP multihilo para manejar requests concurrentes del navegador."""
    daemon_threads = True   # los hilos no bloquean el Ctrl+C


def make_handler(html_bytes: bytes, app_js_bytes: bytes, style_css_bytes: bytes):
    """
    Crea una clase handler con los assets cacheados en su closure.
    Usamos una fábrica porque BaseHTTPRequestHandler no permite constructor custom
    sin sobreescribir __init__ (que tiene signatura fija).
    """
    # Estado de rate limiting compartido entre hilos via closure
    _state            = {"last_refresh": 0.0}
    _refresh_lock     = threading.Lock()
    _REFRESH_COOLDOWN = 60  # segundos mínimos entre actualizaciones

    class IPSAHandler(http.server.BaseHTTPRequestHandler):

        def do_GET(self):
            """Procesa todas las peticiones GET del navegador."""
            parsed = urllib.parse.urlparse(self.path)
            path   = parsed.path

            if path in ("/", "/index.html"):
                # Servir el HTML principal con el payload de inicialización embebido
                self._respond(200, "text/html; charset=utf-8", html_bytes)

            elif path == "/static/app.js":
                self._respond(200, "application/javascript; charset=utf-8", app_js_bytes)

            elif path == "/static/style.css":
                self._respond(200, "text/css; charset=utf-8", style_css_bytes)

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
            """Envía una respuesta HTTP completa con headers de seguridad."""
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
                    # 'unsafe-inline' eliminado de script-src: los datos de
                    # inicialización van en <script type="application/json">,
                    # que no es ejecutable y no requiere este permiso.
                    "default-src 'self'; "
                    "script-src 'self' https://cdnjs.cloudflare.com https://static.cloudflareinsights.com; "
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
# 5. ARRANQUE DEL SERVIDOR Y APERTURA DEL NAVEGADOR
# ─────────────────────────────────────────────────────────────────────────────

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

handler    = make_handler(html_bytes, _APP_JS_BYTES, _STYLE_CSS_BYTES)
_bind_host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
server     = ThreadingHTTPServer((_bind_host, port), handler)

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
