"""
Trading EN VIVO (intradía) sobre cruce de medias 9/12.

Dos tareas que corren en paralelo (ver run_bot.py):

  radar()  cada 10 min: renueva el watchlist (screener: acciones/CEDEARs líquidos
           de IOL) y cada ~20 min le pide a Gemini que marque qué papeles tienen
           noticias alcistas ahora. Eso ordena a quién se vigila primero.

  tick()   cada 30 s (solo con el mercado abierto): lee la cotización en vivo de
           los papeles "en foco" (posiciones abiertas + los mejores candidatos),
           arma barras de 1 minuto con esos precios y decide:

    COMPRA  si la media 9 cruzó por encima de la 12 hace ≤ 3 barras, el precio está
            por encima de la media 9, el papel sube en el día, hay cupo libre y
            alcanza para 1 unidad. Gemini confirma con las noticias antes de
            comprar (puede frenar, no puede operar contra la señal).
    VENTA   (solo posiciones que abrió el bot) si la 9 cruza por debajo de la 12,
            o si el precio cae STOP_LOSS_PCT % bajo el precio de compra. Las ventas
            no esperan a Gemini: la velocidad importa.

Las barras viven en memoria: al arrancar el bot hace falta juntar BARRAS_MIN
barras (≈13 min) antes de poder operar un papel.
"""
import json
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import bot_engine
import db
import fuentes_noticias
import gemini_decision
import iol_client
import screener

ART = timezone(timedelta(hours=-3))      # hora de Argentina (sin horario de verano)
ABRE = (11, 0)                           # el mercado abre 11:00
NO_COMPRAR_DESDE = (16, 30)              # sin compras nuevas en los últimos minutos
CIERRA = (17, 0)

BARRA_SEGUNDOS = 300                     # tamaño de barra para las medias (300 = 5 min; 60 = 1 min)
BARRAS_MIN = 13                          # barras necesarias para medir 9/12 y detectar cruce
CRUCE_RECIENTE_BARRAS = 3                # el cruce alcista debe ser de hace ≤ 3 barras
STOP_LOSS_PCT = 2.0                      # vende si cae 2 % bajo el precio de compra (corte de pérdida)
TOMA_GANANCIA_PCT = 3.0                  # vende si sube 3 % sobre el precio de compra

# --- Rentabilidad neta de comisiones (tarifa de IOL, tramo Gold) --------------------
# Comisión 0.5 % + IVA 21 % = 0.605 %  y  derechos de mercado 0.05 % + IVA = 0.06 %
# -> ~0.67 % por operación. IOL NO cobra la venta de un título comprado ese mismo día
# (mismo símbolo, plazo y moneda): una vuelta completa en el día paga solo la compra;
# si la posición pasa la noche, paga compra y venta. Si pasás a Platinum/Black, bajá esto.
COMISION_LADO_PCT = 0.67
MARGEN_NETO_PCT = 0.3                    # ganancia neta mínima que debe dejar una venta "en positivo"
STOP_MOVIL_EXTRA_PCT = 0.2               # el stop móvil se activa 0.2 % sobre el piso rentable
STOP_MOVIL_RETROCESO_PCT = 0.6           # y vende si retrocede 0.6 % desde su máximo (nunca bajo el piso)
MARGEN_ENTRADA_PCT = 0.15                # el cruce alcista debe separar 9 y 12 al menos 0.15 %
MAX_POSICIONES = bot_engine.MAX_POSICIONES
MAX_COMPRAS_DIA = 10                     # tope de compras por día (frena el sobre-operar)
FOCO_MAX = 12                            # cuántos papeles se leen en cada tick
ENFRIAMIENTO_SEG = 600                   # tras vender, no recomprar ese papel por 10 min
VETO_SEG = 600                           # si Gemini frena una compra, no se re-consulta por 10 min
RADAR_NOTICIAS_SEG = 3600               # 1 vez por hora: el plan gratuito de Gemini permite pocos pedidos por día

_lock = threading.Lock()
_buffers = {}            # simbolo -> {"barras": deque, "clave": int, "ultimo": float}
_watchlist = []          # lo que dejó el screener (ordenado por score)
_sesgo_noticias = []     # papeles con noticias alcistas según Gemini
_ultimo_radar_noticias = 0.0
_pendientes = {}         # simbolo -> hora de la última compra (orden recién enviada)
_enfriamiento = {}       # simbolo -> hora de la última venta
_vetos = {}              # simbolo -> hora en que Gemini frenó la compra
_compras_hoy = {"fecha": None, "n": 0}
_latido = {"t": 0.0}
_maximos = {}           # simbolo -> precio máximo visto desde la compra (para el stop móvil)


# ----------------------------------------------------------------- utilidades
def _ahora():
    return datetime.now(timezone.utc).astimezone(ART)


def _mercado_abierto(ahora):
    if ahora.weekday() >= 5:
        return False
    hm = (ahora.hour, ahora.minute)
    return ABRE <= hm < CIERRA


def _log(msg):
    print(f"[{_ahora():%H:%M:%S}] {msg}", flush=True)


def _latir(msg, cada=300):
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
        ask = puntas[0].get("precioVenta") or None   # a este precio vende el mercado (nosotros compramos)
        bid = puntas[0].get("precioCompra") or None  # a este precio compra el mercado (nosotros vendemos)
    var = cot.get("variacion")
    if var is None and cot.get("ultimoCierre") and precio:
        var = (precio / cot["ultimoCierre"] - 1) * 100
    return {"precio": precio, "ask": ask, "bid": bid, "var": var}


def _actualizar_barras(simbolo, precio, ahora):
    b = _buffers.setdefault(simbolo, {"barras": deque(maxlen=60), "clave": None, "ultimo": None})
    clave = int(ahora.timestamp() // BARRA_SEGUNDOS)
    if b["clave"] is None:
        b["clave"] = clave
    elif clave != b["clave"]:
        if b["ultimo"] is not None:
            b["barras"].append(b["ultimo"])  # cierra la barra anterior con su último precio
        b["clave"] = clave
    b["ultimo"] = precio
    return b["barras"]


def _sma(valores, n):
    return sum(valores[-n:]) / n


def _estado(barras):
    """SMA 9/12 sobre barras cerradas y edad (en barras) del último cruce alcista."""
    v = list(barras)
    if len(v) < BARRAS_MIN:
        return None
    edad = None
    for k in range(CRUCE_RECIENTE_BARRAS):
        sub = v[: len(v) - k]
        prev = sub[:-1]
        if len(prev) < 12:
            break
        if _sma(sub, 9) > _sma(sub, 12) and _sma(prev, 9) <= _sma(prev, 12):
            edad = k
            break
    s9, s12 = _sma(v, 9), _sma(v, 12)
    return {"sma9": s9, "sma12": s12, "alcista": s9 > s12, "edad_cruce_alcista": edad}


def _cantidades_del_bot():
    """{simbolo: cantidad neta que COMPRÓ EL BOT y no vendió}. Solo eso se puede vender."""
    neto = {}
    for m in db.get_movimientos(5000):
        if m.get("simulado"):
            continue
        signo = 1 if m.get("tipo") == "compra" else -1
        neto[m["simbolo"]] = neto.get(m["simbolo"], 0) + signo * m.get("cantidad", 0)
    return {s: q for s, q in neto.items() if q > 0}


def _fechas_compra():
    """{simbolo: fecha (YYYY-MM-DD) de la última compra real del bot}."""
    fechas = {}
    for m in db.get_movimientos(5000):          # vienen ordenados de más nuevo a más viejo
        if m.get("simulado") or m.get("tipo") != "compra":
            continue
        fechas.setdefault(m["simbolo"], str(m.get("fecha", ""))[:10])
    return fechas


def _armar_foco(ocupadas):
    foco = list(ocupadas)
    for s in _sesgo_noticias + _watchlist:
        if s not in foco:
            foco.append(s)
    return foco[: max(FOCO_MAX, len(ocupadas))]


# ----------------------------------------------------------------- radar
def _radar_noticias():
    global _sesgo_noticias, _ultimo_radar_noticias
    _ultimo_radar_noticias = time.time()
    conocidos = [m["simbolo"] for m in screener._cache.get("metricas", [])] or list(_watchlist)
    if not conocidos:
        return
    try:
        contexto = fuentes_noticias.contexto_general_mercado()
        prompt = (
            "Sos un analista de trading intradía del mercado argentino. Con las noticias de hoy, "
            "elegí hasta 5 símbolos de esta lista que tengan un catalizador alcista claro AHORA "
            f"(resultados, noticias de la empresa/sector, contexto favorable): {conocidos}.\n\n"
            f"Noticias del mercado:\n{contexto}\n\n"
            'Respondé SOLO un JSON: {"simbolos": ["XXX", ...], "razon": "una frase"}. '
            'Si no hay ninguno claro, {"simbolos": [], "razon": "..."}.'
        )
        resp = gemini_decision._generar(prompt)
        texto = resp.text.strip().strip("`").replace("json", "", 1).strip()
        data = json.loads(texto)
        _sesgo_noticias = [s for s in data.get("simbolos", []) if s in conocidos][:5]
        _log(f"Radar de noticias: {_sesgo_noticias or 'sin catalizadores claros'} - {data.get('razon', '')}")
    except Exception as e:
        _log(f"Radar de noticias falló (se sigue sin sesgo): {e}")


def radar():
    global _watchlist
    _log("Radar: renovando watchlist con el screener...")
    _watchlist = screener.ejecutar_screener()
    _log(f"Radar: watchlist = {_watchlist}")
    if _mercado_abierto(_ahora()) and time.time() - _ultimo_radar_noticias >= RADAR_NOTICIAS_SEG:
        _radar_noticias()


# ----------------------------------------------------------------- tick en vivo
def tick():
    ahora = _ahora()
    if not _mercado_abierto(ahora):
        _latir("Mercado cerrado (abre lun-vie 11:00-17:00). El bot espera.", cada=1800)
        return
    with _lock:
        _tick(ahora)


def _tick(ahora):
    hoy = ahora.strftime("%Y-%m-%d")
    if _compras_hoy["fecha"] != hoy:
        _compras_hoy.update(fecha=hoy, n=0)
        _buffers.clear()

    cfg = db.get_config()
    mercado_de = {p["simbolo"]: p.get("mercado", "bCBA") for p in cfg.get("papeles", [])}
    asignado_de = {p["simbolo"]: p.get("monto_asignado", 0) for p in cfg.get("papeles", [])}
    capital_total = cfg.get("capital_total", 0)

    cartera = bot_engine._portafolio()
    del_bot = _cantidades_del_bot()
    fechas_compra = _fechas_compra()
    now = time.time()
    abiertas = {s for s in del_bot if cartera.get(s, (0,))[0] > 0}
    pendientes = {s for s, t in _pendientes.items() if now - t < 180}
    ocupadas = abiertas | pendientes

    # --- lectura en vivo del foco
    lecturas = {}
    for s in _armar_foco(ocupadas):
        try:
            lec = _leer(s, mercado_de.get(s, "bCBA"))
        except Exception as e:
            _log(f"[{s}] no pude leer la cotización: {e}")
            continue
        if lec["precio"]:
            lecturas[s] = lec
            _actualizar_barras(s, lec["precio"], ahora)

    _latir(
        f"Vigilando {len(lecturas)} papeles | posiciones del bot: {sorted(abiertas) or 'ninguna'} | "
        f"compras hoy: {_compras_hoy['n']}/{MAX_COMPRAS_DIA} | "
        f"con barras suficientes: {sum(1 for s in lecturas if len(_buffers[s]['barras']) >= BARRAS_MIN)}/{len(lecturas)}"
    )

    # --- ventas (solo lo que compró el bot)
    for s in sorted(abiertas):
        lec = lecturas.get(s)
        if not lec:
            continue
        cant_cartera, ppc, _ = cartera[s]
        cantidad = min(del_bot[s], cant_cartera)
        est = _estado(_buffers[s]["barras"])
        precio = lec["precio"]
        _maximos[s] = max(_maximos.get(s, ppc or precio), precio)
        # costo total de la vuelta: compra + (venta solo si NO es el mismo día de la compra)
        costo_pct = COMISION_LADO_PCT * (1 if fechas_compra.get(s) == hoy else 2)
        piso = ppc * (1 + (costo_pct + MARGEN_NETO_PCT) / 100) if ppc else None   # precio desde el que la venta deja ganancia NETA
        arma_pct = costo_pct + MARGEN_NETO_PCT + STOP_MOVIL_EXTRA_PCT
        motivo = None
        if ppc and precio <= ppc * (1 - STOP_LOSS_PCT / 100):
            motivo = f"stop-loss: ${precio:,.2f} cayó más de {STOP_LOSS_PCT}% bajo su compra (${ppc:,.2f})"
        elif ppc and precio >= ppc * (1 + TOMA_GANANCIA_PCT / 100):
            motivo = f"toma de ganancia: ${precio:,.2f} subió {TOMA_GANANCIA_PCT}% sobre su compra (${ppc:,.2f})"
        elif ppc and _maximos[s] >= ppc * (1 + arma_pct / 100):
            # stop móvil: sigue al máximo, pero nunca vende por debajo del piso rentable
            stop = max(_maximos[s] * (1 - STOP_MOVIL_RETROCESO_PCT / 100), piso)
            if precio <= stop:
                motivo = (f"stop móvil: máximo ${_maximos[s]:,.2f}, retrocedió a ${precio:,.2f} "
                          f"(ganancia neta aprox. {(precio / ppc - 1) * 100 - costo_pct:+.2f}%)")
        elif est and not est["alcista"]:
            if piso and precio >= piso:
                motivo = (f"cruce bajista en vivo con ganancia neta: SMA9 {est['sma9']:,.2f} < SMA12 {est['sma12']:,.2f}, "
                          f"${precio:,.2f} sobre el piso rentable ${piso:,.2f}")
            else:
                _latir_cruce = f"[{s}] cruce bajista pero ${precio:,.2f} no cubre comisiones (piso ${piso:,.2f}): se mantiene, esperando recuperación o stop-loss."
                _latir(_latir_cruce, cada=120)
        if motivo:
            precio_venta = lec["bid"] or precio
            _log(f"[{s}] VENDER {cantidad}: {motivo}")
            bot_engine._ejecutar(s, cantidad, precio_venta, "venta", mercado_de.get(s, "bCBA"), motivo)
            _enfriamiento[s] = now
            _pendientes.pop(s, None)
            _maximos.pop(s, None)

    # --- compras
    if (ahora.hour, ahora.minute) >= NO_COMPRAR_DESDE:
        return
    if _compras_hoy["n"] >= MAX_COMPRAS_DIA:
        return
    cupos = MAX_POSICIONES - len(ocupadas)
    if cupos <= 0:
        return

    candidatos = []
    for s, lec in lecturas.items():
        if s in ocupadas or cartera.get(s, (0,))[0] > 0:
            continue
        if now - _enfriamiento.get(s, 0) < ENFRIAMIENTO_SEG or now - _vetos.get(s, 0) < VETO_SEG:
            continue
        est = _estado(_buffers[s]["barras"])
        if not est or not est["alcista"] or est["edad_cruce_alcista"] is None:
            continue
        if lec["precio"] <= est["sma9"]:
            continue
        if lec["var"] is not None and lec["var"] <= 0:
            continue
        fuerza = (est["sma9"] - est["sma12"]) / est["sma12"]
        if fuerza * 100 < MARGEN_ENTRADA_PCT:
            continue  # cruce demasiado justo: ruido
        prioridad = 1 if s in _sesgo_noticias else 0
        candidatos.append((prioridad, fuerza, s, est, lec))
    candidatos.sort(key=lambda c: (c[0], c[1]), reverse=True)

    for _, fuerza, s, est, lec in candidatos:
        if cupos <= 0 or _compras_hoy["n"] >= MAX_COMPRAS_DIA:
            break
        monto = asignado_de.get(s, 0) or (capital_total / MAX_POSICIONES if capital_total else 0)
        precio_compra = lec["ask"] or lec["precio"]
        if monto <= 0:
            _log(f"[{s}] No compra: no hay capital total configurado.")
            return
        cantidad = int(monto // precio_compra)
        if cantidad < 1:
            _log(f"[{s}] Cruce alcista pero ${monto:,.0f} por posición no alcanza para 1 unidad a ${precio_compra:,.2f}.")
            _vetos[s] = now
            continue
        medias = {
            "simbolo": s,
            "marco_temporal": f"intradía en vivo, barras de {BARRA_SEGUNDOS}s",
            "fuentes": {"iol": {"sma9": round(est["sma9"], 2), "sma12": round(est["sma12"], 2), "senal": "alcista"}},
            "senal_combinada": "alcista",
            "cruce_alcista_hace_barras": est["edad_cruce_alcista"],
            "precio": lec["precio"],
            "variacion_dia_pct": lec["var"],
            "noticias_alcistas_en_radar": s in _sesgo_noticias,
        }
        _log(f"[{s}] cruce alcista en vivo (fuerza {fuerza:.3%}, var día {lec['var']}). Consultando a Gemini...")
        decision = gemini_decision.decidir(s, medias, 0, 0, lec["precio"])
        decision = bot_engine._aplicar_regla_medias(decision, medias, s)
        _log(f"[{s}] decisión final: {decision}")
        if decision["accion"] != "COMPRAR":
            _vetos[s] = time.time()
            continue
        bot_engine._ejecutar(s, cantidad, precio_compra, "compra", mercado_de.get(s, "bCBA"), decision["razon"])
        _pendientes[s] = time.time()
        _compras_hoy["n"] += 1
        cupos -= 1
