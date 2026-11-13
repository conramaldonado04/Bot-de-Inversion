"""
Cliente para la API de InvertirOnline (IOL).

Documentación de referencia: https://api.invertironline.com
(la API es la misma que usa la web/app de IOL; requiere usuario y clave, NO una
api-key separada). Si algún endpoint cambió de forma, la respuesta de error trae
el detalle y hay que ajustar la ruta acá.

Manejo de token: IOL usa OAuth2 "password grant". El access_token dura poco
(~15 min), por eso se cachea en memoria y se renueva con el refresh_token
cuando vence, sin pedir usuario/clave de nuevo.
"""
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.invertironline.com"

_token_cache = {"access_token": None, "refresh_token": None, "expira": 0}


def _pedir_token():
    username = os.environ.get("IOL_USERNAME")
    password = os.environ.get("IOL_PASSWORD")
    if not username or not password:
        raise RuntimeError("Falta IOL_USERNAME / IOL_PASSWORD en el archivo .env")

    resp = requests.post(
        f"{BASE_URL}/token",
        data={"username": username, "password": password, "grant_type": "password"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _guardar_token(data)


def _refrescar_token():
    resp = requests.post(
        f"{BASE_URL}/token",
        data={
            "refresh_token": _token_cache["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        # el refresh token también puede haber vencido -> pedimos uno nuevo desde cero
        _pedir_token()
        return
    _guardar_token(resp.json())


def _guardar_token(data):
    _token_cache["access_token"] = data["access_token"]
    _token_cache["refresh_token"] = data["refresh_token"]
    # restamos 60s de margen de seguridad
    _token_cache["expira"] = time.time() + data.get("expires_in", 900) - 60


def _get_token():
    if not _token_cache["access_token"]:
        _pedir_token()
    elif time.time() >= _token_cache["expira"]:
        _refrescar_token()
    return _token_cache["access_token"]


def _headers():
    return {"Authorization": f"Bearer {_get_token()}"}


def _get(path, params=None):
    resp = requests.get(f"{BASE_URL}{path}", headers=_headers(), params=params, timeout=15)
    if resp.status_code == 401:
        # token vencido o invalidado: pedimos uno nuevo desde cero y reintentamos una vez
        _token_cache["access_token"] = None
        _pedir_token()
        resp = requests.get(f"{BASE_URL}{path}", headers=_headers(), params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_portafolio(pais: str = "argentina"):
    """Tenencias actuales. pais: 'argentina' o 'estados_Unidos'. Reintenta si IOL tarda o corta la conexión."""
    ultimo = None
    for intento in range(3):
        try:
            return _get(f"/api/v2/portafolio/{pais}")
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            ultimo = e
            time.sleep(2)
    raise ultimo


def get_cotizacion(simbolo: str, mercado: str = "bCBA"):
    return _get(f"/api/v2/{mercado}/Titulos/{simbolo}/Cotizacion")


def get_historico(simbolo: str, fecha_desde: str, fecha_hasta: str, mercado: str = "bCBA", ajustada: str = "sinAjustar"):
    """Fechas en formato 'YYYY-MM-DD'. Devuelve una lista de precios diarios."""
    return _get(
        f"/api/v2/{mercado}/Titulos/{simbolo}/Cotizacion/seriehistorica/{fecha_desde}/{fecha_hasta}/{ajustada}"
    )


def get_panel(instrumento: str = "acciones", panel: str = "Todos", pais: str = "argentina"):
    """
    Listado completo de cotizaciones de un instrumento (acciones, cedears, ...)
    tal como lo muestra IOL. Se prueban dos formas de ruta por si la API la
    expone distinta; devuelve la lista de títulos o levanta el último error.
    """
    rutas = [
        f"/api/v2/Cotizaciones/{instrumento}/{panel}/{pais}",
        f"/api/v2/Cotizaciones/{instrumento}/{pais}/{panel}",
    ]
    ultimo_error = None
    for ruta in rutas:
        try:
            data = _get(ruta)
            titulos = data.get("titulos") if isinstance(data, dict) else data
            if titulos:
                return titulos
        except Exception as e:
            ultimo_error = e
    if ultimo_error:
        raise ultimo_error
    return []


def get_operaciones(fecha_desde: str = None, fecha_hasta: str = None, estado: str = None):
    """Historial de operaciones (compras/ventas) registradas en IOL."""
    params = {}
    if fecha_desde:
        params["filtro.fechaDesde"] = fecha_desde
    if fecha_hasta:
        params["filtro.fechaHasta"] = fecha_hasta
    if estado:
        params["filtro.estado"] = estado
    return _get("/api/v2/operaciones", params=params)

def _post(path, body):
    resp = requests.post(f"{BASE_URL}{path}", headers=_headers(), json=body, timeout=15)
    if resp.status_code == 401:
        _token_cache["access_token"] = None
        _pedir_token()
        resp = requests.post(f"{BASE_URL}{path}", headers=_headers(), json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def colocar_orden(simbolo, cantidad, precio, tipo, mercado="bCBA", plazo="t0", validez=None):
    """
    tipo: 'compra' o 'venta'.
    IMPORTANTE: esto ejecuta una orden real con plata real en tu cuenta de IOL.
    Verificá los nombres de campo contra la documentación vigente de IOL antes
    de dejarlo correr en automático sin supervisión, porque el broker puede
    haber cambiado el formato del body.
    """
    endpoint = "/api/v2/operar/Comprar" if tipo == "compra" else "/api/v2/operar/Vender"
    body = {
        "mercado": mercado,
        "simbolo": simbolo,
        "cantidad": cantidad,
        "precio": precio,
        "plazo": plazo,
        "validez": validez or __import__("datetime").datetime.now().strftime("%Y-%m-%d"),
    }
    return _post(endpoint, body)


def cancelar_orden(numero):
    """Cancela una orden pendiente de IOL (DELETE /api/v2/operaciones/{numero})."""
    path = f"/api/v2/operaciones/{numero}"
    resp = requests.delete(f"{BASE_URL}{path}", headers=_headers(), timeout=15)
    if resp.status_code == 401:
        _token_cache["access_token"] = None
        _pedir_token()
        resp = requests.delete(f"{BASE_URL}{path}", headers=_headers(), timeout=15)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return resp.text
