"""
Balances: arma, a partir del historial de movimientos, las operaciones cerradas (compra -> venta)
con su resultado, y los totales por mes y por año.

Costo de compra de cada venta, en este orden:
  1) FIFO contra las compras registradas en el historial (lo que compró el bot),
  2) campo "ppc_compra" guardado en la venta (el bot lo guarda desde ahora para papeles que compraste vos),
  3) "costos_previos" de balances_config.json (para ventas anteriores de papeles que compraste fuera del bot).
Si no hay ninguna, la operación queda "sin costo" y no suma al resultado.

Comisiones: estimación de COMISION_PCT por lado (compra y venta) sobre el importe.
"""
import json
import os
from collections import defaultdict, deque

COMISION_PCT = 0.67  # % aprox. por lado en IOL (estimado, no es el valor exacto de cada operación)
_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "balances_config.json")


def _cargar_config():
    try:
        with open(_CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _ignorado(m, ignorar):
    f = str(m.get("fecha", ""))
    for r in ignorar:
        if r.get("simbolo") != m.get("simbolo") or (r.get("tipo") and r.get("tipo") != m.get("tipo")):
            continue
        if r.get("desde") or r.get("hasta"):      # rango de fecha/hora: "desde" y "hasta" inclusivos (formato AAAA-MM-DD HH:MM)
            if f >= str(r.get("desde", "")) and f <= str(r.get("hasta", "9999")) + "~":
                return True
        elif f.startswith(str(r.get("fecha", ""))):
            return True
    return False


def calcular(movimientos):
    """
    movimientos: lista de dicts (cualquier orden). Devuelve
    {"movimientos": [...ordenados...], "operaciones": [...cerradas...]}
    """
    cfg = _cargar_config()
    ignorar = cfg.get("ignorar", [])
    previos = {k.upper(): float(v) for k, v in cfg.get("costos_previos", {}).items()}

    base = [m for m in movimientos if not m.get("simulado") and not _ignorado(m, ignorar)]
    movs = list(base)
    for x in cfg.get("compras_extra", []):   # operaciones reales que quedaron sin registro en el historial
        if not any(m.get("simbolo") == x.get("simbolo") and m.get("tipo") == x.get("tipo")
                   and str(m.get("fecha", ""))[:10] == str(x.get("fecha", ""))[:10]
                   and m.get("cantidad") == x.get("cantidad") for m in base):
            movs.append(dict(x))
    movs.sort(key=lambda m: str(m.get("fecha", "")))

    lotes = defaultdict(deque)   # simbolo -> [[cantidad, precio, fecha], ...]
    operaciones = []
    for m in movs:
        s = m.get("simbolo")
        q = float(m.get("cantidad", 0) or 0)
        p = float(m.get("precio", 0) or 0)
        if m.get("tipo") == "compra":
            lotes[s].append([q, p, str(m.get("fecha", ""))])
            continue
        # venta: consume lotes FIFO
        restante = q
        partes = []   # (cantidad, precio_compra, fecha_compra)
        while restante > 1e-9 and lotes[s]:
            lote = lotes[s][0]
            usar = min(restante, lote[0])
            partes.append((usar, lote[1], lote[2]))
            lote[0] -= usar
            restante -= usar
            if lote[0] <= 1e-9:
                lotes[s].popleft()
        if restante > 1e-9:
            costo = m.get("ppc_compra")
            if costo in (None, 0):
                costo = previos.get(str(s).upper())
            partes.append((restante, float(costo) if costo else None, None))
        # una fila por tramo (normalmente una sola)
        for cant, costo, fcompra in partes:
            imp_venta = cant * p
            if costo is None:
                op = dict(simbolo=s, cantidad=cant, fecha_venta=str(m.get("fecha", "")), fecha_compra=fcompra,
                          precio_compra=None, precio_venta=p, bruto=None, comisiones=None, neto=None,
                          pct=None, motivo=m.get("razon", ""))
            else:
                imp_compra = cant * costo
                bruto = imp_venta - imp_compra
                com = (imp_venta + imp_compra) * COMISION_PCT / 100
                op = dict(simbolo=s, cantidad=cant, fecha_venta=str(m.get("fecha", "")), fecha_compra=fcompra,
                          precio_compra=costo, precio_venta=p, bruto=bruto, comisiones=com, neto=bruto - com,
                          pct=(p / costo - 1) * 100, motivo=m.get("razon", ""))
            operaciones.append(op)
    return {"movimientos": movs, "operaciones": operaciones}


def _resumen_vacio():
    return dict(cerradas=0, ganadoras=0, perdedoras=0, sin_costo=0, bruto=0.0, comisiones=0.0, neto=0.0,
                ganancia=0.0, perdida=0.0, compras=0, ventas=0, comprado=0.0, vendido=0.0)


def _sumar(res, ops, movs):
    for m in movs:
        imp = float(m.get("importe", 0) or (float(m.get("cantidad", 0)) * float(m.get("precio", 0))))
        if m.get("tipo") == "compra":
            res["compras"] += 1
            res["comprado"] += imp
        else:
            res["ventas"] += 1
            res["vendido"] += imp
    for o in ops:
        if o["neto"] is None:
            res["sin_costo"] += 1
            continue
        res["cerradas"] += 1
        res["bruto"] += o["bruto"]
        res["comisiones"] += o["comisiones"]
        res["neto"] += o["neto"]
        if o["neto"] >= 0:
            res["ganadoras"] += 1
            res["ganancia"] += o["neto"]
        else:
            res["perdedoras"] += 1
            res["perdida"] += o["neto"]
    res["acierto"] = (res["ganadoras"] / res["cerradas"] * 100) if res["cerradas"] else None
    return res


NOMBRES_MES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
               "septiembre", "octubre", "noviembre", "diciembre"]


def agrupar(datos, vista="mensual"):
    """Devuelve lista de períodos (más nuevo primero) con resumen, operaciones y movimientos."""
    movs, ops = datos["movimientos"], datos["operaciones"]

    def clave(fecha):
        f = str(fecha)[:7] if vista == "mensual" else str(fecha)[:4]
        return f

    periodos = {}
    for m in movs:
        periodos.setdefault(clave(m.get("fecha")), {"ops": [], "movs": []})["movs"].append(m)
    for o in ops:
        periodos.setdefault(clave(o["fecha_venta"]), {"ops": [], "movs": []})["ops"].append(o)

    salida = []
    for k in sorted(periodos, reverse=True):
        p = periodos[k]
        if vista == "mensual" and len(k) == 7 and k[5:7].isdigit():
            etiqueta = f"{NOMBRES_MES[int(k[5:7])].capitalize()} {k[:4]}"
        else:
            etiqueta = k
        resumen = _sumar(_resumen_vacio(), p["ops"], p["movs"])
        item = dict(clave=k, etiqueta=etiqueta, resumen=resumen,
                    operaciones=sorted(p["ops"], key=lambda o: o["fecha_venta"], reverse=True),
                    movimientos=sorted(p["movs"], key=lambda m: str(m.get("fecha", "")), reverse=True))
        if vista == "anual":
            meses = {}
            for o in p["ops"]:
                meses.setdefault(o["fecha_venta"][:7], {"ops": [], "movs": []})["ops"].append(o)
            for m in p["movs"]:
                meses.setdefault(str(m.get("fecha", ""))[:7], {"ops": [], "movs": []})["movs"].append(m)
            item["meses"] = [
                dict(etiqueta=f"{NOMBRES_MES[int(mk[5:7])].capitalize()}", resumen=_sumar(_resumen_vacio(), v["ops"], v["movs"]))
                for mk, v in sorted(meses.items(), reverse=True) if len(mk) == 7 and mk[5:7].isdigit()
            ]
        salida.append(item)
    return salida


def total_general(datos):
    return _sumar(_resumen_vacio(), datos["operaciones"], datos["movimientos"])
