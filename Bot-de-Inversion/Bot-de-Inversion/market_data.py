"""
Cálculo de medias móviles 9/12 cruzando dos fuentes:
- IOL (histórico propio, ya autenticado)
- Yahoo Finance (yfinance, gratis, sin api-key)

Si las dos fuentes coinciden en la dirección de la señal (cruce alcista/bajista),
la confianza es mayor. Si difieren, se lo marcamos a Gemini para que lo pondere.
"""
from datetime import datetime, timedelta
import statistics
import yfinance as yf
import iol_client

# Mapeo simbolo IOL -> ticker de Yahoo Finance.
# Para papeles nuevos que agregue el screener y no estén acá, se usa el
# fallback "<SIMBOLO>.BA" (formato de la bolsa de Bs. As. en Yahoo Finance).
TICKER_YFINANCE = {
    "GGAL": "GGAL", "YPFD": "YPF", "PAMP": "PAM", "BMA": "BMA", "VIST": "VIST",
    "TSLA": "TSLA", "NVDA": "NVDA", "MELI": "MELI", "PLTR": "PLTR", "AMD": "AMD",
}


def _sma(precios, periodo):
    if len(precios) < periodo:
        return None
    return statistics.mean(precios[-periodo:])


def _precios_iol(simbolo, mercado, dias=60):
    hasta = datetime.now().strftime("%Y-%m-%d")
    desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")
    try:
        historico = iol_client.get_historico(simbolo, desde, hasta, mercado=mercado)
        return [h.get("ultimoPrecio") for h in historico if h.get("ultimoPrecio")]
    except Exception:
        return []


def _precios_yfinance(simbolo, dias=60):
    ticker = TICKER_YFINANCE.get(simbolo, f"{simbolo}.BA")
    try:
        hist = yf.Ticker(ticker).history(period=f"{dias}d")
        return list(hist["Close"].dropna().values)
    except Exception:
        return []


def obtener_medias(simbolo: str, mercado: str = "bCBA"):
    """
    Devuelve un dict con la media 9 y 12 de cada fuente disponible, más una
    señal combinada: 'alcista', 'bajista' o 'sin_senal' si no coinciden o
    falta data.
    """
    precios_iol = _precios_iol(simbolo, mercado)
    precios_yf = _precios_yfinance(simbolo)

    resultado = {"simbolo": simbolo, "fuentes": {}}

    for nombre, precios in (("iol", precios_iol), ("yfinance", precios_yf)):
        sma9 = _sma(precios, 9)
        sma12 = _sma(precios, 12)
        if sma9 is None or sma12 is None:
            continue
        resultado["fuentes"][nombre] = {
            "sma9": round(sma9, 2),
            "sma12": round(sma12, 2),
            "senal": "alcista" if sma9 > sma12 else "bajista",
        }

    senales = [f["senal"] for f in resultado["fuentes"].values()]
    if not senales:
        resultado["senal_combinada"] = "sin_datos"
    elif len(set(senales)) == 1:
        resultado["senal_combinada"] = senales[0]
    else:
        resultado["senal_combinada"] = "sin_consenso"  # las fuentes no coinciden

    return resultado