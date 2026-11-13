"""
Estrategia SWING (mediano plazo: días a semanas), reemplaza al trading intradía.

Idea: pocas operaciones, cada una elegida por TENDENCIA + NOTICIAS + TÉCNICO, y
sostenida mientras la historia siga en pie. Así las comisiones de IOL (~0.67 %
por lado) pesan poco frente al movimiento esperado.

Dos cosas corren en cada tick (cada 5 min, solo con el mercado abierto):

  vigilar()   Reglas duras sobre las posiciones que abrió el bot: stop-loss y stop
              móvil (no dependen de ninguna IA). Al llegar al objetivo (+12 %) se
              le consulta a Gemini UNA vez: si noticias y tendencia siguen muy
              alcistas deja correr la ganancia (solo protege el stop móvil); si no
              responde o ve debilidad, vende.

  revision()  3 veces por día (11:30, 13:30 y 15:30):
      1) renueva el watchlist con el screener,
      2) revisa cada posición abierta (con al menos 1 día de tenencia):
           - si la tendencia se rompió (precio bajo la media de 20 y media 9 bajo la 12)
             y la venta deja ganancia neta de comisiones, VENDE (regla fija);
             si todavía no cubre comisiones, mantiene y decide Gemini;
           - Gemini lee noticias + tendencia y decide MANTENER o VENDER (si pasó algo
             en las noticias, vende aunque la ganancia sea chica);
      3) si hay cupo, arma candidatos con tendencia DIARIA de mediano plazo (media 20
         sobre la 50, precio sobre la 20) y timing (media 9 sobre la 12, sin estar
         recalentado), le pide a Gemini que marque
         cuáles tienen catalizadores en las noticias, y le consulta por cada uno
         (COMPRAR / MANTENER con confianza 1-10). Compra solo con COMPRAR y
         confianza >= CONFIANZA_MIN.
      Si Gemini no responde, NO se compra y las posiciones se mantienen (siguen
      protegidas por las reglas duras).

Solo se venden posiciones que compró el bot. Las tenencias previas no se tocan.
"""
import json
import os
import statistics
import time
from datetime import datetime, timedelta, timezone

import bot_engine
import db
import fuentes_noticias
import gemini_decision
import iol_client
import market_data
import screener

ART = timezone(timedelta(hours=-3))      # hora de Argentina
ABRE = (11, 0)
CIERRA = (17, 0)
HORAS_REVISION = [(11, 30), (13, 30), (15, 30)]   # revisiones de compra/venta por día (la última antes de las 16:30)
NO_COMPRAR_DESDE = (16, 30)

MAX_POSICIONES = 6                       # el bot gestiona TODA la cartera de acciones/CEDEARs (propias y compradas por él)
TOPE_POSICION_DIV = 4                    # una compra nueva usa como máximo capital_total / 4
# Instrumentos que el bot NO toca (otra clase de activo: no aplica análisis técnico de acciones).
EXCLUIR_DE_GESTION = {"DICP"}
TIPOS_EXCLUIDOS = ("TITULOSPUBLICOS", "PUBLIC", "BONO", "LETRA", "CAUC", "FONDO", "FCI", "OBLIGACION", "OPCION")
ESPERA_VERIFICAR_COMPRA_SEG = 25         # tras comprar, espera y comprueba que el papel figure en la cartera de IOL
REINTENTOS_VERIFICACION = 6              # si todavía no figura, vuelve a mirar cada PAUSA_VERIFICACION_SEG (~2 min en total)
PAUSA_VERIFICACION_SEG = 15
HORAS_CONCILIAR_COMPRA = 2               # una compra anulada que aparece luego en la cartera se vuelve a registrar
MAX_COMPRAS_DIA = 2                      # operar poco: máximo 2 compras nuevas por día
CANDIDATOS_A_CONSULTAR = 4               # cuántos candidatos se le consultan a Gemini por revisión
CONFIANZA_MIN = 6                        # confianza (1-10) mínima de Gemini para comprar
MAX_EXTENSION_PCT = 6.0                  # no comprar si el precio ya está > 6 % sobre su media de 12 ruedas (recalentado)
MAX_RET5_PCT = 10.0                      # no perseguir: no comprar si subió más de 10 % en 5 ruedas...
MAX_RET20_PCT = 25.0                     # ...o más de 25 % en 20 ruedas
RIESGO_POR_OPERACION_PCT = 1.0           # se arriesga como máximo el 1 % del capital total por operación (tamaño = riesgo / stop)
STOP_MIN_PCT, STOP_MAX_PCT, STOP_VOL_MULT = 5.0, 10.0, 2.5   # stop por volatilidad: 2,5 x movimiento diario medio, entre 5 y 10 %
MIN_DIAS_VENTA_DISCRECIONAL = 1          # Gemini solo puede vender si se tiene desde antes de hoy

# Reglas duras (porcentaje sobre el precio de compra)
STOP_LOSS_PCT = 7.0
TOMA_GANANCIA_PCT = 12.0
STOP_MOVIL_ARMA_PCT = 6.0                # el stop móvil se activa cuando el máximo supera +6 %
STOP_MOVIL_RETROCESO_PCT = 4.0           # y vende si retrocede 4 % desde ese máximo
STOP_MOVIL_ESCALON_PCT = 10.0            # escalonado: si el máximo llegó a +10 % o más...
STOP_MOVIL_RETROCESO_ALTO_PCT = 3.0      # ...el retroceso permitido baja a 3 % (cuida más la ganancia ya hecha)
COMISION_LADO_PCT = 0.67                 # tarifa IOL Gold (comisión + IVA + derechos)
MARGEN_NETO_PCT = 0.3                    # ganancia neta mínima para vender "por cambio de tendencia"
# Posición que pasa la noche: paga compra y venta (~1.34 %). Piso bruto para vender por tendencia:
PISO_VENTA_TENDENCIA_PCT = COMISION_LADO_PCT * 2 + MARGEN_NETO_PCT

SCAN_CADA_SEG = 30 * 60                  # escaneo técnico (sin IA salvo que haya candidato) cada 30 min
SCAN_DESDE = (11, 15)                    # no escanea en los primeros 15 min de la rueda
RADAR_VIDA_DIAS = 5                      # un papel que Gemini sumó al radar por noticias se sigue 5 días
ENFRIAMIENTO_DIAS = 5                    # tras vender un papel (stop o lo que sea) no se lo vuelve a comprar por 5 días
VETO_MIN = 90                            # si Gemini vetó un papel, no se le vuelve a consultar por 90 min
MAX_INTENTOS_REVISION = 4
ESPERA_REINTENTO_SEG = 10 * 60

_revision = {"fecha": None, "hecha": False, "slot": -1, "due": None, "intentos": 0, "ultimo": 0.0}
_compras_hoy = {"fecha": None, "n": 0}
_compras_pendientes = []   # compras anuladas por no verse en la cartera; si aparecen después se re-registran
_ARCHIVO_MAXIMOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maximos.json")


def _cargar_maximos():
    """Los máximos se guardan en disco: si se reinicia el bot (o la PC), el stop móvil no pierde el pico."""
    try:
        with open(_ARCHIVO_MAXIMOS, encoding="utf-8") as f:
            return {k: float(v) for k, v in json.load(f).items()}
    except Exception:
        return {}


_maximos = _cargar_maximos()             # simbolo -> máximo visto desde la compra (stop móvil)


def _retroceso_stop_movil(maximo, ppc):
    """Stop móvil escalonado: 4 % de retroceso hasta +10 % de ganancia máxima; 3 % si el máximo pasó +10 %."""
    if ppc and maximo >= ppc * (1 + STOP_MOVIL_ESCALON_PCT / 100):
        return STOP_MOVIL_RETROCESO_ALTO_PCT
    return STOP_MOVIL_RETROCESO_PCT


def _guardar_maximos():
    try:
        with open(_ARCHIVO_MAXIMOS, "w", encoding="utf-8") as f:
            json.dump(_maximos, f)
    except Exception:
        pass


def _olvidar_maximo(s):
    _maximos.pop(s, None)
    _guardar_maximos()
    if s in _stops:
        _stops.pop(s, None)
        _guardar_stops()


_ARCHIVO_STOPS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stops.json")


def _cargar_stops():
    try:
        with open(_ARCHIVO_STOPS, encoding="utf-8") as f:
            return {k: float(v) for k, v in json.load(f).items()}
    except Exception:
        return {}


_stops = _cargar_stops()                 # simbolo -> % de stop-loss propio de esa posición (según su volatilidad al comprar)


def _stop_de(s):
    return _stops.get(s, STOP_LOSS_PCT)


def _guardar_stops():
    try:
        with open(_ARCHIVO_STOPS, "w", encoding="utf-8") as f:
            json.dump(_stops, f)
    except Exception:
        pass


_dejar_correr = set()                    # papeles que pasaron el objetivo y Gemini dejó correr
_latido = {"t": 0.0}


# ----------------------------------------------------------------- utilidades
def _ahora():
    return datetime.now(timezone.utc).astimezone(ART)


def _mercado_abierto(ahora):
    if ahora.weekday() >= 5:
        return False
    return ABRE <= (ahora.hour, ahora.minute) < CIERRA


def _log(msg):
    print(f"[{_ahora():%H:%M:%S}] {msg}", flush=True)


def _latir(msg, cada=900):
    if time.time() - _latido["t"] >= cada:
        _latido["t"] = time.time()
        _log(msg)


def _leer(simbolo, mercado):
    cot = iol_client.get_cotizacion(simbolo, mercado)
    precio = cot.get("ultimoPrecio") or 0
    puntas = cot.get("puntas") or []
    if isinstance(puntas, dict):
        puntas = [puntas]
    ask = bid = None
    if puntas:
        ask = puntas[0].get("precioVenta") or None
        bid = puntas[0].get("precioCompra") or None
    return {"precio": precio, "ask": ask, "bid": bid}


def _cantidades_del_bot():
    neto = {}
    for m in db.get_movimientos(5000):
        if m.get("simulado"):
            continue
        signo = 1 if m.get("tipo") == "compra" else -1
        neto[m["simbolo"]] = neto.get(m["simbolo"], 0) + signo * m.get("cantidad", 0)
    return {s: q for s, q in neto.items() if q > 0}


_cartera_leida = {"ok": True}   # False si la última lectura de la cartera de IOL falló (timeout, etc.)


def _cartera_estricta():
    """{simbolo: cantidad} de la cartera real de IOL, o None si IOL no responde (para no confundir 'error' con 'vacía')."""
    try:
        data = iol_client.get_portafolio("argentina")
    except Exception as e:
        _log(f"No pude leer la cartera de IOL: {e}")
        return None
    return {(a.get("titulo", {}) or {}).get("simbolo"): a.get("cantidad", 0) or 0 for a in data.get("activos", [])}


def _cartera_coherente(cartera, del_bot):
    """True si la lectura de la cartera sirve para decidir (la última lectura anduvo y no quedó vacía por error)."""
    return _cartera_leida["ok"] and (not del_bot or bool(cartera))


def _gestionadas():
    """
    Posiciones que el bot administra: TODAS las acciones/CEDEARs de la cartera real de IOL
    (las que compró el bot y las que compró el usuario), salvo bonos/cauciones/fondos y EXCLUIR_DE_GESTION.
    Devuelve ({simbolo: cantidad}, {simbolo: mercado}). Si IOL no responde devuelve vacío (no se toca nada).
    """
    try:
        data = iol_client.get_portafolio("argentina")
    except Exception as e:
        _log(f"No pude leer la cartera de IOL: {e}")
        _cartera_leida["ok"] = False
        return {}, {}
    _cartera_leida["ok"] = True
    cant, mercados = {}, {}
    for a in data.get("activos", []):
        t = a.get("titulo", {}) or {}
        s = t.get("simbolo")
        q = a.get("cantidad", 0) or 0
        if not s or q <= 0 or s in EXCLUIR_DE_GESTION:
            continue
        tipo = str(t.get("tipo") or "").upper().replace(" ", "")
        if any(x in tipo for x in TIPOS_EXCLUIDOS):
            continue
        cant[s] = q
        mercados[s] = t.get("mercado") or "bCBA"
    return cant, mercados


def _fechas_compra():
    fechas = {}
    for m in db.get_movimientos(5000):       # de más nuevo a más viejo
        if m.get("simulado") or m.get("tipo") != "compra":
            continue
        fechas.setdefault(m["simbolo"], str(m.get("fecha", ""))[:10])
    return fechas


def _vendidos_recientes(hoy):
    """Papeles vendidos en los últimos ENFRIAMIENTO_DIAS días (por el bot)."""
    out = set()
    try:
        limite = datetime.strptime(hoy, "%Y-%m-%d") - timedelta(days=ENFRIAMIENTO_DIAS)
        for m in db.get_movimientos(300):
            if m.get("simulado") or m.get("tipo") != "venta":
                continue
            try:
                if datetime.strptime(str(m.get("fecha", ""))[:10], "%Y-%m-%d") >= limite:
                    out.add(m.get("simbolo"))
            except ValueError:
                pass
    except Exception:
        pass
    return out


def _compras_de_hoy(hoy):
    return sum(1 for m in db.get_movimientos(200)
               if not m.get("simulado") and m.get("tipo") == "compra" and str(m.get("fecha", ""))[:10] == hoy)


def _json(texto):
    t = (texto or "").strip()
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j == -1:
        raise ValueError(f"respuesta sin JSON: {t[:120]}")
    return json.loads(t[i:j + 1])


def _consultar(prompt):
    """Devuelve el JSON de Gemini (o de la IA de respaldo) o None si todas fallan."""
    try:
        return _json(gemini_decision._generar(prompt).text)
    except Exception as e:
        _log(f"IA no disponible o respuesta inválida: {str(e)[:160]}")
        return None


def _instrucciones():
    try:
        ins = db.get_instrucciones()
    except Exception:
        ins = []
    return "\n".join(f"- {i}" for i in ins) if ins else "(sin instrucciones especiales)"


# ----------------------------------------------------------------- análisis técnico diario
def _tecnico(simbolo, mercado):
    p = [x for x in market_data._precios_iol(simbolo, mercado, dias=120) if x]
    if len(p) < 13:
        return None
    ult = p[-1]
    sma9 = statistics.mean(p[-9:])
    sma12 = statistics.mean(p[-12:])
    sma20 = statistics.mean(p[-20:]) if len(p) >= 20 else None
    sma50 = statistics.mean(p[-50:]) if len(p) >= 50 else None
    ret5 = (ult / p[-6] - 1) * 100 if len(p) >= 6 else None
    ret20 = (ult / p[-21] - 1) * 100 if len(p) >= 21 else None
    max60 = max(p[-60:])
    # volatilidad: movimiento diario medio (valor absoluto) de las últimas 14 ruedas -> stop propio del papel
    movs = [abs(p[i] / p[i - 1] - 1) * 100 for i in range(max(1, len(p) - 14), len(p)) if p[i - 1]]
    vol = statistics.mean(movs) if movs else 0.0
    stop_sug = round(min(STOP_MAX_PCT, max(STOP_MIN_PCT, STOP_VOL_MULT * vol)), 1)
    # MACD (12, 26, 9) sobre cierres diarios
    macd_hist = macd_cruce = None
    if len(p) >= 40:
        def _ema(v, n):
            k, e, out = 2 / (n + 1), v[0], []
            for x in v:
                e = x * k + e * (1 - k)
                out.append(e)
            return out
        e12, e26 = _ema(p, 12), _ema(p, 26)
        linea = [a - b for a, b in zip(e12, e26)]
        senal = _ema(linea, 9)
        hist = [a - b for a, b in zip(linea, senal)]
        macd_hist = hist[-1]
        macd_cruce = hist[-1] > 0 and any(h <= 0 for h in hist[-4:-1])   # cruzó al alza en las últimas 3 ruedas
    t = {
        "rondas_de_historia": len(p),
        "precio": round(ult, 2),
        "sma9_diaria": round(sma9, 2),
        "sma12_diaria": round(sma12, 2),
        "sma20_diaria": round(sma20, 2) if sma20 else None,
        "sma50_diaria": round(sma50, 2) if sma50 else None,
        "tendencia_9_12": "alcista" if sma9 > sma12 else "bajista",
        "precio_vs_sma12_pct": round((ult / sma12 - 1) * 100, 2),
        "retorno_5_ruedas_pct": round(ret5, 2) if ret5 is not None else None,
        "retorno_20_ruedas_pct": round(ret20, 2) if ret20 is not None else None,
        "distancia_al_maximo_60_ruedas_pct": round((ult / max60 - 1) * 100, 2),
        "volatilidad_diaria_pct": round(vol, 2),
        "stop_sugerido_pct": stop_sug,
        "macd_histograma_positivo": (macd_hist > 0) if macd_hist is not None else None,
        "macd_cruce_alcista_reciente": macd_cruce,
    }
    t["tendencia_mediano_plazo_20_50"] = (
        None if not sma50 else ("alcista" if sma20 > sma50 and ult > sma20 else "no alcista"))
    # tendencia rota: el precio perdió la media de 20 Y la 9 quedó bajo la 12
    t["tendencia_rota"] = bool(sma20 and ult < sma20 and sma9 < sma12)
    return t


def _noticias(simbolo):
    try:
        return fuentes_noticias.contexto_para_papel(simbolo, market_data.TICKER_YFINANCE.get(simbolo, simbolo))
    except Exception as e:
        return f"(no pude traer noticias: {e})"


# ----------------------------------------------------------------- prompts
PROMPT_POSICION = """Sos un analista de inversión de MEDIANO PLAZO (swing: días a semanas) del mercado
argentino (acciones y CEDEARs). El bot ya tiene una posición en {simbolo} y revisás si conviene
SEGUIR sosteniéndola. No operes por el ruido de un día: vendé solo si la tesis se rompió (noticias
negativas relevantes, tendencia diaria quebrada, deterioro claro). Si pasó algo en las noticias que
cambia la historia, vendé aunque la ganancia sea chica. Si la tendencia y las noticias siguen muy
alcistas, mantené para dejar correr la ganancia. Stop de pérdida {stop}% y stop móvil los maneja el
sistema aparte (objetivo de referencia +{tp}%).
{nota_objetivo}
Posición: {cantidad} unidades, precio promedio de compra {ppc}, precio actual {precio}
(resultado actual: {resultado}%), tenida desde {desde} ({dias} días).
Técnico diario: {tecnico}

Noticias y contexto:
{noticias}

Instrucciones permanentes del usuario:
{instrucciones}

Respondé SOLO un JSON: {{"accion": "MANTENER" | "VENDER", "confianza": 1-10, "razon": "una o dos frases con tendencia, noticias y técnico"}}
"""

PROMPT_CANDIDATO = """Sos un analista de inversión de MEDIANO PLAZO (swing: días a semanas) del mercado
argentino (acciones y CEDEARs). Evaluá si COMPRAR {simbolo} hoy para sostenerlo varios días o semanas.
Combiná tres cosas: la TENDENCIA, las NOTICIAS/catalizadores y el ANÁLISIS TÉCNICO diario. El papel ya
pasó el filtro duro técnico (tendencia alcista o arranque confirmado por MACD) y no está sobreextendido. Si la tendencia
y el técnico son sólidos, COMPRÁ (confianza 6 o más) salvo que haya noticias NEGATIVAS relevantes para el
papel o el mercado (riesgo de caída, mala noticia de la empresa, sacudón macro fuerte, resultados que ya
dispararon el precio y pueden devolverse). Tu rol es de VETO: las noticias NO son el motivo para comprar
(el precio ya las descontó, llegan tarde); solo sirven para frenar una compra riesgosa. La FALTA de noticias
alcistas o de datos de noticias es neutral. El sistema protege con stop de {stop}% (ajustado a la volatilidad
del papel) y objetivo de +{tp}%.

Técnico diario: {tecnico}
Precio actual: {precio}
¿Estuvo en el radar de noticias alcistas de hoy?: {en_radar}

Noticias y contexto:
{noticias}

Instrucciones permanentes del usuario:
{instrucciones}

Respondé SOLO un JSON: {{"accion": "COMPRAR" | "MANTENER", "confianza": 1-10, "razon": "una o dos frases con tendencia, noticias y técnico"}}
"""

PROMPT_RADAR = """Sos un analista de mediano plazo (días a semanas) del mercado argentino y de los CEDEARs que
cotizan en IOL. Con las noticias actuales, proponé hasta 6 símbolos con un catalizador o tendencia alcista
claros para las próximas semanas (resultados, sector, macro favorable). Pueden ser de esta lista de seguimiento:
{conocidos}
o OTROS que no estén ahí (acciones argentinas o CEDEARs; usá el ticker tal como cotiza en IOL, p. ej. NVDA, GGAL).
Solo proponé papeles que realmente existan; no inventes tickers.

Noticias del mercado:
{contexto}

Respondé SOLO un JSON: {{"simbolos": ["XXX", ...], "razon": "una frase"}}. Si no hay ninguno claro: {{"simbolos": [], "razon": "..."}}.
"""

_ARCHIVO_RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "radar.json")
_panel_cache = {"t": 0.0, "simbolos": set()}


def _cargar_radar():
    try:
        with open(_ARCHIVO_RADAR, encoding="utf-8") as f:
            return dict(json.load(f))
    except Exception:
        return {}


_radar_vivo = _cargar_radar()            # simbolo -> fecha (YYYY-MM-DD) en que Gemini lo sumó por noticias


def _guardar_radar():
    try:
        with open(_ARCHIVO_RADAR, "w", encoding="utf-8") as f:
            json.dump(_radar_vivo, f)
    except Exception:
        pass


def _panel_validos():
    """Símbolos que existen en el panel completo de IOL (acciones + CEDEARs). Vacío si no se pudo leer."""
    if _panel_cache["simbolos"] and time.time() - _panel_cache["t"] < 3600:
        return _panel_cache["simbolos"]
    simbolos = set()
    for instrumento in ("acciones", "cedears"):
        try:
            for x in iol_client.get_panel(instrumento):
                if x.get("simbolo") and x.get("ultimoPrecio"):
                    simbolos.add(str(x["simbolo"]).upper())
        except Exception as e:
            _log(f"Radar: no pude leer el panel '{instrumento}' de IOL ({e})")
    if simbolos:
        _panel_cache.update(t=time.time(), simbolos=simbolos)
    return simbolos


def _reponer_radar(hoy):
    """Mantiene en el watchlist los papeles del radar de noticias que siguen vigentes (el screener los quita)."""
    vivos = []
    for s, f in list(_radar_vivo.items()):
        try:
            edad = (datetime.strptime(hoy, "%Y-%m-%d") - datetime.strptime(f, "%Y-%m-%d")).days
        except ValueError:
            edad = 99
        if edad > RADAR_VIDA_DIAS:
            _radar_vivo.pop(s, None)
            continue
        try:
            db.agregar_papel(s)
        except Exception as e:
            _log(f"[{s}] no pude reponerlo al watchlist ({e})")
        vivos.append(s)
    _guardar_radar()
    return vivos


def _radar_noticias(conocidos):
    """Gemini lee las noticias y propone papeles (de la lista o nuevos). Los nuevos y válidos en IOL se suman al watchlist."""
    try:
        contexto = fuentes_noticias.contexto_general_mercado()
    except Exception as e:
        _log(f"Radar de noticias: no pude traer el contexto ({e})")
        return []
    data = _consultar(PROMPT_RADAR.format(conocidos=list(conocidos), contexto=contexto))
    if not data:
        return []
    validos = _panel_validos()
    hoy = _ahora().strftime("%Y-%m-%d")
    elegidos, nuevos = [], []
    for s in data.get("simbolos", []):
        s = str(s).upper().strip()
        if s in conocidos:
            elegidos.append(s)
        elif s in validos and s not in EXCLUIR_DE_GESTION:
            try:
                db.agregar_papel(s)
            except Exception as e:
                _log(f"[{s}] no pude sumarlo al watchlist ({e})")
                continue
            _radar_vivo[s] = hoy
            nuevos.append(s)
            elegidos.append(s)
        else:
            _log(f"Radar: Gemini propuso '{s}' pero no figura en el panel de IOL; se ignora.")
    if nuevos:
        _guardar_radar()
    _log(f"Radar de noticias: {elegidos or 'sin catalizadores claros'} (nuevos en el watchlist: {nuevos or 'ninguno'}) - {data.get('razon', '')}")
    return elegidos


def _consultar_objetivo(s, mercado, cantidad, ppc, precio):
    """Llegó al objetivo: ¿vender o dejar correr? Devuelve True si hay que vender."""
    resultado = round((precio / ppc - 1) * 100, 2) if ppc else 0
    t = _tecnico(s, mercado)
    data = _consultar(PROMPT_POSICION.format(
        simbolo=s, stop=_stop_de(s), tp=TOMA_GANANCIA_PCT, cantidad=cantidad, ppc=ppc, precio=precio,
        resultado=resultado, desde="(ver historial)", dias="?",
        tecnico=json.dumps(t, ensure_ascii=False), noticias=_noticias(s), instrucciones=_instrucciones(),
        nota_objetivo=(f"\nATENCIÓN: la posición ya alcanzó el objetivo (+{TOMA_GANANCIA_PCT}%). Decidí si conviene "
                       "VENDER y asegurar, o MANTENER para dejar correr la ganancia porque tendencia y noticias "
                       "siguen MUY alcistas (el stop móvil protege la ganancia).\n")))
    if not data:
        _log(f"[{s}] objetivo alcanzado y la IA no responde: se vende para asegurar la ganancia.")
        return True
    accion, razon = data.get("accion"), data.get("razon", "")
    _log(f"[{s}] objetivo alcanzado ({resultado:+.2f}%): {accion} (confianza {data.get('confianza')}) - {razon}")
    if accion == "MANTENER":
        _dejar_correr.add(s)
        return False
    return True


# ----------------------------------------------------------------- vigilancia (reglas duras)
def vigilar(ahora=None):
    ahora = ahora or _ahora()
    cfg = db.get_config()
    mercado_de = {p["simbolo"]: p.get("mercado", "bCBA") for p in cfg.get("papeles", [])}
    cartera = bot_engine._portafolio()
    del_bot, mercados_cartera = _gestionadas()
    for s_, m_ in mercados_cartera.items():
        mercado_de.setdefault(s_, m_)
    if not _cartera_coherente(cartera, del_bot):
        _log("Cartera de IOL no disponible: se saltea la vigilancia de este ciclo (se reintenta en el próximo).")
        return
    _conciliar_compras(cartera)
    abiertas = [s for s in del_bot if cartera.get(s, (0,))[0] > 0]
    _latir(f"Swing: posiciones gestionadas: {sorted(abiertas) or 'ninguna'} | revisiones de hoy: {_revision['slot'] + 1}/{len(HORAS_REVISION)}")

    for s in sorted(abiertas):
        cant_cartera, ppc, _ = cartera[s]
        cantidad = min(del_bot[s], cant_cartera)
        try:
            lec = _leer(s, mercado_de.get(s, "bCBA"))
        except Exception as e:
            _log(f"[{s}] no pude leer la cotización: {e}")
            continue
        precio = lec["precio"]
        if not precio or not ppc:
            continue
        _nuevo_max = max(_maximos.get(s, ppc), precio)
        if _maximos.get(s) != _nuevo_max:
            _maximos[s] = _nuevo_max
            _guardar_maximos()
        motivo = None
        if precio <= ppc * (1 - _stop_de(s) / 100):
            motivo = f"stop-loss: ${precio:,.2f} cayó más de {_stop_de(s):g}% bajo su compra (${ppc:,.2f})"
        elif precio >= ppc * (1 + TOMA_GANANCIA_PCT / 100) and s not in _dejar_correr:
            if _consultar_objetivo(s, mercado_de.get(s, "bCBA"), cantidad, ppc, precio):
                motivo = f"toma de ganancia: ${precio:,.2f} subió {TOMA_GANANCIA_PCT}% sobre su compra (${ppc:,.2f})"
        elif _maximos[s] >= ppc * (1 + STOP_MOVIL_ARMA_PCT / 100) and precio <= _maximos[s] * (1 - _retroceso_stop_movil(_maximos[s], ppc) / 100):
            motivo = (f"stop móvil ({_retroceso_stop_movil(_maximos[s], ppc):g}% desde el máximo): máximo ${_maximos[s]:,.2f}, retrocedió a ${precio:,.2f} "
                      f"(resultado aprox. {(precio / ppc - 1) * 100:+.1f}% antes de comisiones)")
        if motivo:
            _log(f"[{s}] VENDER {cantidad}: {motivo}")
            if not bot_engine._ejecutar(s, cantidad, lec["bid"] or precio, "venta", mercado_de.get(s, "bCBA"), motivo, ppc_compra=ppc):
                _log(f"[{s}] la venta no se pudo ejecutar; se reintenta en el próximo ciclo.")
                continue
            _olvidar_maximo(s)
            _dejar_correr.discard(s)


# ----------------------------------------------------------------- revisión diaria
def _revisar_posiciones(hoy, cartera, del_bot, fechas, mercado_de):
    ok = True
    for s in sorted(del_bot):
        cant_cartera, ppc, ult = cartera.get(s, (0, 0, 0))
        if cant_cartera <= 0:
            continue
        mercado = mercado_de.get(s, "bCBA")
        try:
            precio = _leer(s, mercado)
        except Exception as e:
            _log(f"[{s}] revisión: no pude leer la cotización ({e})")
            ok = False
            continue
        p = precio["precio"] or ult
        desde = fechas.get(s)
        if desde:
            try:
                dias = (datetime.strptime(hoy, "%Y-%m-%d") - datetime.strptime(desde, "%Y-%m-%d")).days
            except ValueError:
                dias = 0
        else:
            desde, dias = "(compra previa, fuera del bot)", MIN_DIAS_VENTA_DISCRECIONAL   # posición del usuario
        resultado = round((p / ppc - 1) * 100, 2) if ppc else 0
        # Regla dura: stop-loss antes de consultar a Gemini.
        if ppc and resultado <= -_stop_de(s):
            motivo = f"stop-loss en revisión: {resultado:+.2f}% (límite -{_stop_de(s):g}%)"
            _log(f"[{s}] VENDER {min(del_bot[s], cant_cartera)}: {motivo}")
            if not bot_engine._ejecutar(s, min(del_bot[s], cant_cartera), precio["bid"] or p, "venta", mercado, motivo, ppc_compra=ppc):
                _log(f"[{s}] la venta no se pudo ejecutar; se reintenta en la próxima vigilancia.")
                continue
            _olvidar_maximo(s)
            _dejar_correr.discard(s)
            continue
        t = _tecnico(s, mercado)
        # Regla fija: la tendencia se rompió y la venta deja ganancia neta de comisiones -> vender.
        if t and t.get("tendencia_rota") and dias >= MIN_DIAS_VENTA_DISCRECIONAL and resultado >= PISO_VENTA_TENDENCIA_PCT:
            motivo = (f"cambio de tendencia: precio ${p:,.2f} bajo su media de 20 ruedas (${t['sma20_diaria']}) y media 9 bajo la 12; "
                      f"resultado {resultado:+.2f}% antes de comisiones (cubre costos)")
            _log(f"[{s}] VENDER {min(del_bot[s], cant_cartera)}: {motivo}")
            if not bot_engine._ejecutar(s, min(del_bot[s], cant_cartera), precio["bid"] or p, "venta", mercado, motivo, ppc_compra=ppc):
                _log(f"[{s}] la venta no se pudo ejecutar; se reintenta en la próxima revisión o vigilancia.")
                continue
            _olvidar_maximo(s)
            _dejar_correr.discard(s)
            continue
        nota = ""
        if t and t.get("tendencia_rota"):
            nota = (f"\nNOTA: la tendencia diaria se rompió pero la ganancia ({resultado:+.2f}%) todavía no cubre comisiones "
                    f"(~{PISO_VENTA_TENDENCIA_PCT:.2f}%). Vendé solo si las noticias o el deterioro lo justifican; "
                    "si no, MANTENER esperando recuperación (el stop de pérdida protege).\n")
        elif resultado >= TOMA_GANANCIA_PCT:
            nota = (f"\nNOTA: ya superó el objetivo de +{TOMA_GANANCIA_PCT}%. Mantené solo si tendencia y noticias siguen "
                    "MUY alcistas (el stop móvil protege); si hay debilidad, VENDER.\n")
        data = _consultar(PROMPT_POSICION.format(
            simbolo=s, stop=_stop_de(s), tp=TOMA_GANANCIA_PCT, cantidad=cant_cartera, ppc=ppc, precio=p,
            resultado=resultado, desde=desde, dias=dias, tecnico=json.dumps(t, ensure_ascii=False),
            noticias=_noticias(s), instrucciones=_instrucciones(), nota_objetivo=nota))
        if not data:
            ok = False
            continue
        accion = data.get("accion")
        razon = data.get("razon", "")
        _log(f"[{s}] revisión de posición ({resultado:+.2f}%, {dias} d): {accion} (confianza {data.get('confianza')}) - {razon}")
        if accion == "VENDER":
            if dias < MIN_DIAS_VENTA_DISCRECIONAL:
                _log(f"[{s}] Gemini sugiere vender pero se compró hoy: se mantiene (solo actúan las reglas duras).")
                continue
            cantidad = min(del_bot[s], cant_cartera)
            if not bot_engine._ejecutar(s, cantidad, precio["bid"] or p, "venta", mercado,
                                        f"revisión de mediano plazo: {razon}", ppc_compra=ppc):
                _log(f"[{s}] la venta no se pudo ejecutar; se reintenta más tarde.")
                continue
            _olvidar_maximo(s)
            del_bot = dict(del_bot)
    return ok


_radar_hoy = set()
_vetos = {}                               # simbolo -> hora (time.time) en que Gemini lo vetó


def _buscar_candidatos(watchlist, cartera, mercado_de, con_radar=True):
    global _radar_hoy
    watchlist = list(dict.fromkeys(watchlist))
    if con_radar:
        _radar_hoy = set(_radar_noticias(watchlist) or [])
        watchlist = list(dict.fromkeys(watchlist + [s for s in _radar_hoy if s not in watchlist]))
    candidatos = []
    enfriando = _vendidos_recientes(_ahora().strftime("%Y-%m-%d"))
    for s in watchlist:
        if cartera.get(s, (0,))[0] > 0:
            continue
        if s in enfriando:
            continue
        if any(p["doc"].get("simbolo") == s for p in _compras_pendientes):
            continue          # ya hay una orden de compra de este papel sin confirmar
        if time.time() - _vetos.get(s, 0) < VETO_MIN * 60:
            continue
        t = _tecnico(s, mercado_de.get(s, "bCBA"))
        if not t:
            continue
        # Dos setups: (A) tendencia confirmada (20/50 y 9/12 alcistas) o (B) ARRANQUE: cruce alcista reciente del MACD
        # con el precio sobre su media de 20 (entra antes, sin esperar que la 20 cruce la 50).
        tendencia = (t["tendencia_mediano_plazo_20_50"] == "alcista" and t["tendencia_9_12"] == "alcista"
                     and t["precio_vs_sma12_pct"] > 0)
        sma20 = t.get("sma20_diaria")
        arranque = bool(t.get("macd_cruce_alcista_reciente") and t.get("macd_histograma_positivo")
                        and sma20 and t["precio"] > sma20)
        if not (tendencia or arranque):
            continue
        if t["precio_vs_sma12_pct"] > MAX_EXTENSION_PCT:
            _log(f"[{s}] alcista pero extendido ({t['precio_vs_sma12_pct']}% sobre su media de 12, tope {MAX_EXTENSION_PCT:g}%): se saltea.")
            continue
        r5, r20 = t.get("retorno_5_ruedas_pct"), t.get("retorno_20_ruedas_pct")
        if (r5 is not None and r5 > MAX_RET5_PCT) or (r20 is not None and r20 > MAX_RET20_PCT):
            _log(f"[{s}] ya corrió demasiado (5 ruedas {r5}%, 20 ruedas {r20}%): no se persigue.")
            continue
        fuerza = (t["sma9_diaria"] / t["sma12_diaria"] - 1) * 100
        candidatos.append((arranque, fuerza, s, t))
    candidatos.sort(key=lambda c: (c[0], c[1]), reverse=True)
    _log(f"Candidatos técnicos: {[(c[2], 'arranque MACD' if c[0] else 'tendencia') for c in candidatos]}")
    return candidatos[:CANDIDATOS_A_CONSULTAR]


def _anular_compra(s, cantidad, precio, desde_fecha, restituir=True):
    """Borra el registro de una compra que IOL no ejecutó (el bot lo anota apenas envía la orden)."""
    try:
        doc = db.get_db().movimientos.find_one_and_delete({"simbolo": s, "tipo": "compra", "cantidad": cantidad,
                                                           "precio": precio, "fecha": {"$gte": desde_fecha}})
        _log(f"[{s}] registro de compra anulado ({1 if doc else 0}).")
        if doc and restituir:
            doc.pop("_id", None)
            _compras_pendientes.append({"doc": doc, "hasta": time.time() + HORAS_CONCILIAR_COMPRA * 3600})
    except Exception as e:
        _log(f"[{s}] no pude anular el registro de compra: {e}")


def _cancelar_compras_pendientes(s):
    """Cancela las órdenes de COMPRA sin ejecutar de un papel (evita que se ejecuten más tarde a precio viejo o duplicadas).
    Devuelve True si no quedó ninguna pendiente (o no había)."""
    try:
        hoy = _ahora().strftime("%Y-%m-%d")
        ops = iol_client.get_operaciones(fecha_desde=hoy, fecha_hasta=hoy)
        if isinstance(ops, dict):
            ops = ops.get("operaciones") or ops.get("items") or [ops]
        pend = [o for o in ops if o.get("simbolo") == s and str(o.get("tipo", "")).lower() == "compra"
                and str(o.get("estado", "")).lower() in bot_engine.ESTADOS_PENDIENTES]
    except Exception as e:
        _log(f"[{s}] no pude listar las órdenes pendientes para cancelarlas ({e})")
        return False
    todo_ok = True
    for o in pend:
        try:
            iol_client.cancelar_orden(o.get("numero"))
            _log(f"[{s}] orden de compra #{o.get('numero')} sin ejecutar: cancelada.")
        except Exception as e:
            todo_ok = False
            _log(f"[{s}] no pude cancelar la orden #{o.get('numero')}: {e}")
    return todo_ok


def _diagnosticar_orden(s):
    """Para entender por qué IOL no ejecutó una orden: deja en el log lo que IOL dice de las operaciones de hoy de ese papel."""
    try:
        hoy = _ahora().strftime("%Y-%m-%d")
        ops = iol_client.get_operaciones(fecha_desde=hoy, fecha_hasta=hoy)
        if isinstance(ops, dict):
            ops = ops.get("operaciones") or ops.get("items") or [ops]
        propias = [o for o in ops if s in json.dumps(o, ensure_ascii=False)]
        if not propias:
            _log(f"[{s}] diagnóstico: IOL no muestra ninguna operación de hoy para este papel.")
        for o in propias[-3:]:
            _log(f"[{s}] diagnóstico IOL: {json.dumps(o, ensure_ascii=False)[:500]}")
    except Exception as e:
        _log(f"[{s}] diagnóstico: no pude consultar las operaciones de IOL ({e})")


def _conciliar_compras(cartera):
    """Si una compra anulada aparece luego en la cartera de IOL (la orden tardó en ejecutarse), se vuelve a registrar."""
    for p in list(_compras_pendientes):
        s = p["doc"]["simbolo"]
        if cartera.get(s, (0,))[0] > 0:
            try:
                db.registrar_movimiento(dict(p["doc"]))
                _compras_hoy["n"] += 1
                _log(f"[{s}] la compra anulada sí se ejecutó en IOL: registro restituido.")
            except Exception as e:
                _log(f"[{s}] no pude restituir el registro de compra: {e}")
                continue
            _compras_pendientes.remove(p)
        elif time.time() > p["hasta"]:
            _log(f"[{s}] la compra anulada nunca apareció en la cartera: se descarta.")
            _compras_pendientes.remove(p)


def _comprar(hoy, candidatos, cfg, del_bot, mercado_de, cartera):
    capital_total = cfg.get("capital_total", 0)
    asignado_de = {p["simbolo"]: p.get("monto_asignado", 0) for p in cfg.get("papeles", [])}
    cupos = MAX_POSICIONES - len(del_bot)
    # El capital total es el techo de lo invertido en posiciones gestionadas (valuadas a precio actual).
    invertido = sum(q * (cartera.get(s, (0, 0, 0))[2] or cartera.get(s, (0, 0, 0))[1] or 0) for s, q in del_bot.items())
    _log(f"Capital gestionado: ${invertido:,.0f} de ${capital_total:,.0f}.")
    ok = True
    for arranque, fuerza, s, t in candidatos:
        if cupos <= 0 or _compras_hoy["n"] >= MAX_COMPRAS_DIA:
            break
        mercado = mercado_de.get(s, "bCBA")
        tope = asignado_de.get(s, 0) or (capital_total / TOPE_POSICION_DIV if capital_total else 0)
        stop_s = t.get("stop_sugerido_pct", STOP_LOSS_PCT)
        if capital_total:   # tamaño por riesgo: la pérdida si salta el stop no supera RIESGO_POR_OPERACION_PCT del capital
            tope = min(tope, capital_total * RIESGO_POR_OPERACION_PCT / stop_s)
        monto = min(tope, capital_total - invertido)
        if tope <= 0:
            _log(f"[{s}] No compra: no hay capital total configurado.")
            return ok
        if monto <= 0:
            _log(f"[{s}] No compra: ya hay ${invertido:,.0f} invertidos (capital total ${capital_total:,.0f}).")
            return ok
        try:
            lec = _leer(s, mercado)
        except Exception as e:
            _log(f"[{s}] no pude leer la cotización: {e}")
            continue
        precio = lec["ask"] or lec["precio"]
        if not precio or int(monto // precio) < 1:
            _log(f"[{s}] ${monto:,.0f} por posición no alcanza para 1 unidad a ${precio:,.2f}.")
            continue
        data = _consultar(PROMPT_CANDIDATO.format(
            simbolo=s, stop=t.get('stop_sugerido_pct', STOP_LOSS_PCT), tp=TOMA_GANANCIA_PCT, tecnico=json.dumps(t, ensure_ascii=False),
            precio=precio, en_radar="sí" if s in _radar_hoy else "no", noticias=_noticias(s), instrucciones=_instrucciones()))
        if not data:
            ok = False
            continue
        try:
            confianza = float(data.get("confianza", 0))
        except (TypeError, ValueError):
            confianza = 0
        _log(f"[{s}] decisión: {data.get('accion')} (confianza {confianza}) - {data.get('razon', '')}")
        if data.get("accion") != "COMPRAR" or confianza < CONFIANZA_MIN:
            _vetos[s] = time.time()
            continue
        cantidad = int(monto // precio)
        # El análisis (noticias + IA) tarda 1-2 minutos: se vuelve a leer la punta vendedora justo antes de enviar la orden,
        # para que el precio límite no quede viejo y la orden se ejecute al instante.
        try:
            lec2 = _leer(s, mercado)
            nuevo = lec2["ask"] or lec2["precio"]
        except Exception as e:
            nuevo = None
            _log(f"[{s}] no pude releer la cotización antes de comprar ({e}); se usa el precio del análisis.")
        if nuevo:
            if nuevo > precio * 1.02:
                _log(f"[{s}] el precio subió de ${precio:,.2f} a ${nuevo:,.2f} (más de 2%) mientras se analizaba: no se compra.")
                continue
            precio = nuevo
            cantidad = int(monto // precio)
            if cantidad < 1:
                _log(f"[{s}] con el precio actual (${precio:,.2f}) ya no alcanza para 1 unidad.")
                continue
        desde_fecha = _ahora().strftime("%Y-%m-%d %H:%M")
        if not bot_engine._ejecutar(s, cantidad, precio, "compra", mercado,
                                    f"mediano plazo (confianza {confianza:g}): {data.get('razon', '')}"):
            _log(f"[{s}] la orden de compra no se envió o IOL la rechazó (mirá el motivo arriba); no se cuenta.")
            _vetos[s] = time.time()      # no insistir (ni gastar IA) con el mismo papel durante un rato
            continue
        time.sleep(ESPERA_VERIFICAR_COMPRA_SEG)
        cart_v = _cartera_estricta()
        for _ in range(REINTENTOS_VERIFICACION):
            if cart_v is None or cart_v.get(s, 0) > 0:
                break
            time.sleep(PAUSA_VERIFICACION_SEG)
            cart_v = _cartera_estricta()
        if cart_v is None:
            _log(f"[{s}] AVISO: no pude verificar la compra porque IOL no respondió; la dejo registrada. Revisá en IOL que se haya ejecutado.")
        elif cart_v.get(s, 0) <= 0:
            _log(f"[{s}] AVISO: la compra no figura en la cartera de IOL tras ~2 min (orden pendiente o rechazada). "
                 "No se cuenta como posición por ahora; si aparece después se registra sola. Revisá las órdenes en IOL.")
            _diagnosticar_orden(s)
            cancelada = _cancelar_compras_pendientes(s)
            _anular_compra(s, cantidad, precio, desde_fecha, restituir=not cancelada)
            _vetos[s] = time.time()
            continue
        _maximos[s] = precio
        _guardar_maximos()
        _stops[s] = stop_s
        _guardar_stops()
        _compras_hoy["n"] += 1
        cupos -= 1
        invertido += cantidad * precio
    return ok


def revision(ahora=None):
    ahora = ahora or _ahora()
    hoy = ahora.strftime("%Y-%m-%d")
    if _revision["fecha"] != hoy:
        _revision.update(fecha=hoy, hecha=False, slot=-1, due=None, intentos=0, ultimo=0.0)
        _compras_hoy.update(fecha=hoy, n=_compras_de_hoy(hoy))
    # revisión que corresponde ahora: la última franja horaria ya alcanzada
    due = max((i for i, h in enumerate(HORAS_REVISION) if (ahora.hour, ahora.minute) >= h), default=-1)
    if due <= _revision["slot"] or due < 0:
        return
    if _revision["due"] != due:
        _revision.update(due=due, intentos=0, ultimo=0.0)
    if _revision["intentos"] >= MAX_INTENTOS_REVISION or time.time() - _revision["ultimo"] < ESPERA_REINTENTO_SEG:
        return
    _revision["intentos"] += 1
    _revision["ultimo"] = time.time()
    _log(f"=== Revisión {due + 1}/{len(HORAS_REVISION)} de mediano plazo (intento {_revision['intentos']}) ===")

    try:
        watchlist = screener.ejecutar_screener()
    except Exception as e:
        _log(f"Screener falló ({e}); se usa el watchlist actual.")
        watchlist = [p["simbolo"] for p in db.get_config().get("papeles", [])]

    watchlist = list(dict.fromkeys(list(watchlist) + _reponer_radar(hoy)))
    cfg = db.get_config()
    mercado_de = {p["simbolo"]: p.get("mercado", "bCBA") for p in cfg.get("papeles", [])}
    cartera = bot_engine._portafolio()
    del_bot, mercados_cartera = _gestionadas()
    for s_, m_ in mercados_cartera.items():
        mercado_de.setdefault(s_, m_)
    if not _cartera_coherente(cartera, del_bot):
        _log("Cartera de IOL no disponible: no se revisa ni se compra ahora; se reintenta en 10 min.")
        return
    fechas = _fechas_compra()

    ok = _revisar_posiciones(hoy, cartera, del_bot, fechas, mercado_de)
    cartera = bot_engine._portafolio()
    del_bot, _ = _gestionadas()              # puede haber cambiado por ventas
    if not _cartera_coherente(cartera, del_bot):
        _log("Cartera de IOL no disponible tras revisar posiciones: no se compra en esta revisión; se reintenta en 10 min.")
        return

    if (ahora.hour, ahora.minute) < NO_COMPRAR_DESDE and _compras_hoy["n"] < MAX_COMPRAS_DIA \
            and len(del_bot) < MAX_POSICIONES:
        candidatos = _buscar_candidatos(watchlist, cartera, mercado_de)
        ok = _comprar(hoy, candidatos, cfg, del_bot, mercado_de, cartera) and ok
    else:
        _log(f"Sin compras: posiciones {len(del_bot)}/{MAX_POSICIONES}, compras hoy {_compras_hoy['n']}/{MAX_COMPRAS_DIA}.")

    _scan["ultimo"] = time.time()            # recién hecha una revisión completa: el escaneo espera 30 min
    if ok:
        _revision.update(hecha=True, slot=due)
        _log(f"=== Revisión {due + 1}/{len(HORAS_REVISION)} completa ===")
    else:
        _log("=== Revisión incompleta (IA o datos no disponibles); se reintenta en 20 min ===")


_scan = {"ultimo": 0.0}


def escaneo_tecnico(ahora=None):
    """Cada 30 min mira el watchlist solo con el filtro técnico (sin IA); si aparece un candidato, recién ahí consulta a Gemini (veto)."""
    ahora = ahora or _ahora()
    hm = (ahora.hour, ahora.minute)
    if hm < SCAN_DESDE or hm >= NO_COMPRAR_DESDE or time.time() - _scan["ultimo"] < SCAN_CADA_SEG:
        return
    hoy = ahora.strftime("%Y-%m-%d")
    if _compras_hoy["fecha"] != hoy:
        _compras_hoy.update(fecha=hoy, n=_compras_de_hoy(hoy))
    _scan["ultimo"] = time.time()
    cfg = db.get_config()
    watchlist = [p["simbolo"] for p in cfg.get("papeles", [])]
    mercado_de = {p["simbolo"]: p.get("mercado", "bCBA") for p in cfg.get("papeles", [])}
    cartera = bot_engine._portafolio()
    del_bot, mercados_cartera = _gestionadas()
    for s_, m_ in mercados_cartera.items():
        mercado_de.setdefault(s_, m_)
    if not _cartera_coherente(cartera, del_bot):
        return
    if _compras_hoy["n"] >= MAX_COMPRAS_DIA or len(del_bot) >= MAX_POSICIONES:
        return
    _log(f"--- Escaneo técnico ({len(watchlist)} papeles) ---")
    candidatos = _buscar_candidatos(watchlist, cartera, mercado_de, con_radar=False)
    if candidatos:
        _comprar(hoy, candidatos, cfg, del_bot, mercado_de, cartera)


def tick():
    ahora = _ahora()
    if not _mercado_abierto(ahora):
        _latir("Mercado cerrado (abre lun-vie 11:00-17:00). El bot espera.", cada=1800)
        return
    vigilar(ahora)
    revision(ahora)
    escaneo_tecnico(ahora)
