"""
Orquesta un ciclo completo del bot (estrategia de cruce de medias 9/12):

1. El screener renueva el watchlist (acciones y CEDEARs líquidos de IOL).
2. Se calcula la señal 9/12 de cada papel con la serie de IOL.
3. COMPRAS: se juntan los papeles con señal ALCISTA que todavía no se tienen,
   se ordenan por fuerza de la señal, se descartan los que no se pueden pagar
   con el monto de una posición, y se compran los mejores hasta ocupar los
   cupos libres (MAX_POSICIONES). El capital se reparte entre las posiciones
   permitidas, no entre todo el watchlist.
4. VENTAS: para las posiciones abiertas por el bot con señal BAJISTA.
5. Gemini confirma o frena cada operación (MANTENER) con noticias e
   instrucciones; no puede operar contra la señal (ver _aplicar_regla_medias).

MODO_SIMULACION: si está en True, hace TODO el proceso y lo registra en el
historial, pero no llama a iol_client.colocar_orden.
"""
import time
from datetime import datetime

import db
import iol_client
import market_data
import gemini_decision
import screener

MODO_SIMULACION = False  # False = opera con plata real (activado por el usuario el 28/9)
MAX_POSICIONES = 3       # cuántos papeles como máximo maneja el bot a la vez


def _portafolio():
    """{simbolo: (cantidad, ppc, ultimoPrecio)} con una sola consulta a IOL."""
    try:
        data = iol_client.get_portafolio("argentina")
        return {
            a.get("titulo", {}).get("simbolo"): (a.get("cantidad", 0), a.get("ppc", 0), a.get("ultimoPrecio", 0))
            for a in data.get("activos", [])
        }
    except Exception as e:
        print(f"No pude leer la cartera de IOL: {e}", flush=True)
        return {}


def _precio_mercado(simbolo, mercado):
    """Último precio de mercado en IOL (sirve aunque el papel no esté en cartera)."""
    try:
        return iol_client.get_cotizacion(simbolo, mercado).get("ultimoPrecio") or 0
    except Exception as e:
        print(f"[{simbolo}] no pude obtener la cotización de IOL: {e}", flush=True)
        return 0


def _posiciones_del_bot():
    """Símbolos que el bot compró (real) y todavía no vendió por completo."""
    neto = {}
    for m in db.get_movimientos(5000):
        if m.get("simulado"):
            continue
        signo = 1 if m.get("tipo") == "compra" else -1
        neto[m["simbolo"]] = neto.get(m["simbolo"], 0) + signo * m.get("cantidad", 0)
    return {s for s, q in neto.items() if q > 0}


def _senal_iol(medias):
    return medias.get("fuentes", {}).get("iol", {})


def _aplicar_regla_medias(decision, medias, simbolo):
    """
    Regla dura (no depende de Gemini): solo se COMPRA con señal alcista y solo
    se VENDE con señal bajista, ambas medidas sobre la serie de IOL.
    """
    senal = _senal_iol(medias).get("senal")
    accion = decision["accion"]
    if accion == "COMPRAR" and senal != "alcista":
        print(f"[{simbolo}] Regla de medias: Gemini quiso COMPRAR pero la señal IOL es '{senal}'. Se mantiene.", flush=True)
        return {"accion": "MANTENER", "razon": f"Bloqueado por regla: compra sin señal alcista ({senal}). {decision.get('razon', '')}"}
    if accion == "VENDER" and senal != "bajista":
        print(f"[{simbolo}] Regla de medias: Gemini quiso VENDER pero la señal IOL es '{senal}'. Se mantiene.", flush=True)
        return {"accion": "MANTENER", "razon": f"Bloqueado por regla: venta sin señal bajista ({senal}). {decision.get('razon', '')}"}
    return decision


def _fuerza(medias):
    f = _senal_iol(medias)
    if not f.get("sma12"):
        return 0
    return (f["sma9"] - f["sma12"]) / f["sma12"]


def ciclo():
    print(f"[{datetime.now()}] Renovando watchlist con el screener...", flush=True)
    papeles_activos = screener.ejecutar_screener()
    print(f"[{datetime.now()}] Watchlist actual: {papeles_activos}", flush=True)

    cfg = db.get_config()
    capital_total = cfg.get("capital_total", 0)
    mercado_por_simbolo = {p["simbolo"]: p.get("mercado", "bCBA") for p in cfg.get("papeles", [])}
    cartera = _portafolio()
    abiertas = _posiciones_del_bot()

    # ---------- 1) VENTAS: posiciones del bot con señal bajista ----------
    for simbolo in sorted(abiertas):
        mercado = mercado_por_simbolo.get(simbolo, "bCBA")
        medias = market_data.obtener_medias(simbolo, mercado)
        cantidad, ppc, precio = cartera.get(simbolo, (0, 0, 0))
        if cantidad <= 0 or _senal_iol(medias).get("senal") != "bajista":
            continue
        decision = gemini_decision.decidir(
            simbolo, medias, cantidad, ppc, precio, ticker_yf=market_data.TICKER_YFINANCE.get(simbolo)
        )
        decision = _aplicar_regla_medias(decision, medias, simbolo)
        print(f"[{simbolo}] posición abierta, señal bajista -> decisión final: {decision}", flush=True)
        if decision["accion"] == "VENDER":
            _ejecutar(simbolo, cantidad, precio or _precio_mercado(simbolo, mercado), "venta", mercado, decision["razon"])
            abiertas.discard(simbolo)

    # ---------- 2) COMPRAS: candidatos con señal alcista ----------
    cupos = MAX_POSICIONES - len(abiertas)
    if cupos <= 0:
        print(f"Cupos completos ({len(abiertas)}/{MAX_POSICIONES} posiciones abiertas): {sorted(abiertas)}. No se buscan compras.", flush=True)
        return

    candidatos = []
    for simbolo in papeles_activos:
        if simbolo in abiertas or cartera.get(simbolo, (0,))[0] > 0:
            continue  # ya lo tenemos
        mercado = mercado_por_simbolo.get(simbolo, "bCBA")
        medias = market_data.obtener_medias(simbolo, mercado)
        if _senal_iol(medias).get("senal") == "alcista":
            candidatos.append((simbolo, mercado, medias, _fuerza(medias)))
        else:
            print(f"[{simbolo}] señal IOL={_senal_iol(medias).get('senal')} -> no es candidato.", flush=True)

    candidatos.sort(key=lambda c: c[3], reverse=True)
    print(f"Candidatos alcistas (de más a menos fuertes): {[c[0] for c in candidatos]}", flush=True)

    # Monto por posición: capital dividido por las posiciones permitidas.
    monto_posicion = capital_total / MAX_POSICIONES if capital_total else 0
    compradas = 0
    for simbolo, mercado, medias, fuerza in candidatos:
        if compradas >= cupos:
            break
        asignado = next((p.get("monto_asignado", 0) for p in cfg.get("papeles", []) if p["simbolo"] == simbolo), 0)
        monto = asignado if asignado > 0 else monto_posicion
        precio = _precio_mercado(simbolo, mercado)
        if monto <= 0:
            print(f"[{simbolo}] No compra: no hay capital total configurado (Configuración).", flush=True)
            return
        if not precio or int(monto // precio) < 1:
            print(f"[{simbolo}] Descartado: ${monto:,.0f} por posición no alcanza para 1 unidad a ${precio:,.2f}.", flush=True)
            continue
        cantidad = int(monto // precio)
        decision = gemini_decision.decidir(
            simbolo, medias, 0, 0, precio, ticker_yf=market_data.TICKER_YFINANCE.get(simbolo)
        )
        decision = _aplicar_regla_medias(decision, medias, simbolo)
        print(f"[{simbolo}] alcista (fuerza {fuerza:.2%}) -> decisión final: {decision}", flush=True)
        if decision["accion"] == "COMPRAR":
            _ejecutar(simbolo, cantidad, precio, "compra", mercado, decision["razon"])
            compradas += 1


ESTADOS_PENDIENTES = ("iniciada", "en_proceso", "parcialmente_terminada")


def _orden_ok(resp):
    """IOL contesta {'numeroOperacion': N} cuando acepta la orden; si rechaza, una lista de errores o {'ok': False, ...}."""
    return isinstance(resp, dict) and resp.get("ok") is not False and (resp.get("ok") is True or resp.get("numeroOperacion") is not None)


def _cancelar_ventas_pendientes(simbolo):
    """Cancela las órdenes de VENTA pendientes de este papel (quedaron con un precio límite viejo y reservan las acciones).
    Devuelve cuántas canceló."""
    n = 0
    try:
        hoy = datetime.now().strftime("%Y-%m-%d")
        ops = iol_client.get_operaciones(fecha_desde=hoy, fecha_hasta=hoy)
        if isinstance(ops, dict):
            ops = ops.get("operaciones") or ops.get("items") or []
        for o in ops:
            if o.get("simbolo") == simbolo and str(o.get("tipo", "")).lower() == "venta" \
                    and str(o.get("estado", "")).lower() in ESTADOS_PENDIENTES:
                try:
                    iol_client.cancelar_orden(o.get("numero"))
                    n += 1
                    print(f"[{simbolo}] orden de venta pendiente #{o.get('numero')} cancelada (precio límite viejo).", flush=True)
                except Exception as e:
                    print(f"[{simbolo}] no pude cancelar la orden #{o.get('numero')}: {e}", flush=True)
    except Exception as e:
        print(f"[{simbolo}] no pude consultar órdenes pendientes: {e}", flush=True)
    return n


def _ejecutar(simbolo, cantidad, precio, tipo, mercado, razon, ppc_compra=None):
    reemplazo = False
    if not MODO_SIMULACION:
        if tipo == "venta" and _cancelar_ventas_pendientes(simbolo):
            reemplazo = True
            time.sleep(2)
        try:
            resp = iol_client.colocar_orden(simbolo, cantidad, precio, tipo, mercado=mercado)
        except Exception as e:
            print(f"[{simbolo}] ERROR al colocar orden real: {e}", flush=True)
            return False
        print(f"[{simbolo}] respuesta de IOL: {str(resp)[:300]}", flush=True)
        if not _orden_ok(resp):
            print(f"[{simbolo}] IOL RECHAZÓ la orden de {tipo}: {resp}", flush=True)
            return False
        if reemplazo:
            # la orden vieja quedó anulada: se borra su registro para no duplicar la venta
            try:
                db.get_db().movimientos.find_one_and_delete(
                    {"simbolo": simbolo, "tipo": "venta", "fecha": {"$gte": datetime.now().strftime("%Y-%m-%d")}},
                    sort=[("fecha", -1)])
            except Exception as e:
                print(f"[{simbolo}] no pude borrar el registro de la venta reemplazada: {e}", flush=True)

    db.registrar_movimiento({
        "fecha": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "simbolo": simbolo,
        "tipo": tipo,
        "cantidad": cantidad,
        "precio": precio,
        "importe": round(cantidad * precio, 2),
        "razon": razon,
        "simulado": MODO_SIMULACION,
        **({"ppc_compra": ppc_compra} if (tipo == "venta" and ppc_compra) else {}),
    })
    etiqueta = "(SIMULADO) " if MODO_SIMULACION else ""
    print(f"{etiqueta}{tipo.upper()} {cantidad} {simbolo} a ${precio}", flush=True)
    return True
