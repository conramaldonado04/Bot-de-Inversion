"""
Screener: arma solo la lista de papeles a seguir. Trae de IOL el listado
completo de acciones y CEDEARs, se queda con los más líquidos, mide volatilidad
y volumen reciente de cada uno y deja en el watchlist (colección 'config' en
Mongo) los que más se destacan. No hay lista fija: el universo sale del panel
de IOL. Si IOL no devuelve el panel, usa como respaldo una lista fija chica.

Fuente de datos: IOL (mismo mercado y moneda en la que opera el bot). Yahoo
Finance queda solo como último respaldo para los papeles de la lista fija.
Las métricas se cachean para no repetir ~40 consultas en cada ciclo.
"""
import statistics
import time
from datetime import datetime, timedelta

import yfinance as yf

import db
import iol_client

# Respaldo si no se puede leer el panel de IOL. Clave = símbolo IOL, valor = ticker Yahoo.
UNIVERSO = {
    "GGAL": "GGAL", "YPFD": "YPF", "PAMP": "PAM", "BMA": "BMA", "VIST": "VIST",
    "TXAR": "TXAR.BA", "ALUA": "ALUA.BA", "CRES": "CRES.BA", "TGSU2": "TGSU2.BA",
    "TRAN": "TRAN.BA", "CEPU": "CEPU", "EDN": "EDN", "LOMA": "LOMA", "SUPV": "SUPV",
    "BBAR": "BBAR", "COME": "COME.BA", "MIRG": "MIRG.BA",
    "TSLA": "TSLA", "NVDA": "NVDA", "MELI": "MELI", "PLTR": "PLTR", "AMD": "AMD",
    "AAPL": "AAPL", "MSFT": "MSFT", "GOOGL": "GOOGL", "AMZN": "AMZN", "KO": "KO",
}

INSTRUMENTOS = ("acciones", "cedears")  # paneles de IOL que se recorren
CANTIDAD_A_PROMOVER = 20   # cuántos papeles como máximo mantiene activos el bot
PRE_FILTRO = 40            # los más líquidos del panel a los que se les mide la historia
CACHE_SEGUNDOS = 3600      # cada cuánto se re-miden las métricas

_cache = {"hora": 0.0, "metricas": []}


def _calcular(simbolo, cierres, volumenes):
    cierres = [c for c in cierres if c]
    if len(cierres) < 10:
        return None
    retornos = [(cierres[i] / cierres[i - 1] - 1) for i in range(1, len(cierres))]
    volatilidad = statistics.pstdev(retornos)
    volumenes = [v for v in volumenes if v is not None]
    volumen_promedio = statistics.mean(volumenes) if volumenes else 0
    return {"simbolo": simbolo, "volatilidad": volatilidad, "volumen_promedio": volumen_promedio}


def _metricas_iol(simbolo):
    try:
        hasta = datetime.now().strftime("%Y-%m-%d")
        desde = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
        hist = iol_client.get_historico(simbolo, desde, hasta)
        cierres = [h.get("ultimoPrecio") for h in hist]
        volumenes = [h.get("montoOperado") or h.get("volumenNominal") or 0 for h in hist]
        return _calcular(simbolo, cierres, volumenes)
    except Exception:
        return None


def _metricas_yahoo(simbolo, ticker_yf):
    if not ticker_yf:
        return None
    try:
        hist = yf.Ticker(ticker_yf).history(period="30d")
        cierres = list(hist["Close"].dropna().values)
        volumenes = list(hist["Volume"].dropna().values)
        return _calcular(simbolo, cierres, volumenes)
    except Exception:
        return None


def _candidatos_desde_panel():
    """Símbolos más líquidos de los paneles de IOL. Devuelve [] si no se pudo leer."""
    liquidez = {}
    for instrumento in INSTRUMENTOS:
        try:
            titulos = iol_client.get_panel(instrumento)
        except Exception as e:
            print(f"Screener: no pude leer el panel '{instrumento}' de IOL ({e})", flush=True)
            continue
        for t in titulos:
            simbolo = t.get("simbolo")
            if not simbolo or not t.get("ultimoPrecio"):
                continue
            liquidez[simbolo] = t.get("volumen") or t.get("montoOperado") or t.get("cantidadOperaciones") or 0
    ordenados = sorted(liquidez, key=lambda s: liquidez[s], reverse=True)
    return ordenados[:PRE_FILTRO]


def _medir_universo():
    candidatos = _candidatos_desde_panel()
    if candidatos:
        print(f"Screener: {len(candidatos)} candidatos líquidos tomados del panel de IOL.", flush=True)
    else:
        candidatos = list(UNIVERSO)
        print("Screener: panel de IOL no disponible, uso la lista fija de respaldo.", flush=True)
    metricas = []
    for simbolo in candidatos:
        m = _metricas_iol(simbolo) or _metricas_yahoo(simbolo, UNIVERSO.get(simbolo))
        if m:
            metricas.append(m)
    return metricas


def ejecutar_screener():
    """
    Rankea los candidatos por (volatilidad * volumen normalizado) y actualiza
    el watchlist en Mongo con los mejores CANTIDAD_A_PROMOVER papeles.
    Devuelve la lista de símbolos que quedaron activos. Si no se pudo medir
    ningún papel, NO toca el watchlist y devuelve la lista actual.
    """
    if _cache["metricas"] and time.time() - _cache["hora"] < CACHE_SEGUNDOS:
        metricas = _cache["metricas"]
    else:
        metricas = _medir_universo()
        if metricas:
            _cache["hora"] = time.time()
            _cache["metricas"] = metricas

    if not metricas:
        print("Screener: no se pudo medir ningún papel. Se mantiene el watchlist actual.", flush=True)
        return [p["simbolo"] for p in db.get_config().get("papeles", [])]

    vol_max = max(m["volumen_promedio"] for m in metricas) or 1
    for m in metricas:
        m["score"] = m["volatilidad"] * (m["volumen_promedio"] / vol_max if vol_max else 1)

    ranking = sorted(metricas, key=lambda m: m["score"], reverse=True)
    top = [m["simbolo"] for m in ranking[:CANTIDAD_A_PROMOVER]]

    cfg = db.get_config()
    actuales = {p["simbolo"] for p in cfg.get("papeles", [])}

    for simbolo in top:
        if simbolo not in actuales:
            db.agregar_papel(simbolo)

    # Saca del watchlist los que ya no están en el top. Las tenencias reales
    # las maneja bot_engine; acá el screener solo cuida la lista de seguimiento.
    for simbolo in actuales - set(top):
        db.quitar_papel(simbolo)

    return top
