import os
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash

import db
import iol_client
import asistente
import balances

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "cambiar-esto-en-produccion")


def _tenencias_normalizadas():
    """
    Junta lo que IOL dice que tenés (cantidad, ppc, último precio, %ganancia)
    con la config propia (monto que le asignaste a ese papel en el panel).
    Si la API de IOL falla (ej. .env sin completar todavía), devuelve lista vacía
    y un mensaje de error para no romper la página.
    """
    try:
        data = iol_client.get_portafolio("argentina")
        activos = data.get("activos", [])
    except Exception as e:
        return [], str(e)

    config = db.get_config()
    montos_por_simbolo = {p["simbolo"]: p.get("monto_asignado", 0) for p in config.get("papeles", [])}

    tenencias = []
    for a in activos:
        titulo = a.get("titulo", {})
        simbolo = titulo.get("simbolo", "?")
        cantidad = a.get("cantidad", 0)
        ppc = a.get("ppc", 0)
        ultimo_precio = a.get("ultimoPrecio", 0)
        ganancia_pct = a.get("gananciaPorcentaje", 0)
        ganancia_dinero = a.get("gananciaDinero", 0)
        tenencias.append({
            "simbolo": simbolo,
            "descripcion": titulo.get("descripcion", simbolo),
            "cantidad": cantidad,
            "precio_compra": ppc,
            "precio_actual": ultimo_precio,
            "ganancia_pct": ganancia_pct,
            "ganancia_dinero": ganancia_dinero,
            "monto_asignado": montos_por_simbolo.get(simbolo),
        })
    return tenencias, None


@app.route("/")
def dashboard():
    tenencias, error = _tenencias_normalizadas()
    return render_template("dashboard.html", tenencias=tenencias, error=error)


@app.route("/config", methods=["GET", "POST"])
def config():
    if request.method == "POST":
        accion = request.form.get("accion")

        if accion == "capital_total":
            monto = float(request.form.get("capital_total", 0) or 0)
            db.set_capital_total(monto)
            flash("Capital total actualizado.", "ok")

        elif accion == "monto_papel":
            simbolo = request.form.get("simbolo")
            monto = float(request.form.get("monto", 0) or 0)
            db.set_monto_papel(simbolo, monto)
            flash(f"Monto de {simbolo} actualizado.", "ok")

        elif accion == "agregar_papel":
            simbolo = request.form.get("nuevo_simbolo", "")
            if simbolo:
                agregado = db.agregar_papel(simbolo)
                flash(f"{simbolo.upper()} agregado." if agregado else f"{simbolo.upper()} ya estaba en la lista.", "ok" if agregado else "warn")

        elif accion == "quitar_papel":
            simbolo = request.form.get("simbolo")
            db.quitar_papel(simbolo)
            flash(f"{simbolo} quitado de la lista.", "ok")

        elif accion == "agregar_instruccion":
            texto = request.form.get("nueva_instruccion", "").strip()
            if texto:
                db.agregar_instruccion(texto)
                flash("Instrucción agregada.", "ok")

        elif accion == "quitar_instruccion":
            texto = request.form.get("texto", "")
            db.quitar_instruccion(texto)
            flash("Instrucción quitada.", "ok")

        return redirect(url_for("config"))

    cfg = db.get_config()
    capital_total = cfg.get("capital_total", 0)
    papeles = cfg.get("papeles", [])
    instrucciones = cfg.get("instrucciones", [])
    monto_asignado_total = sum(p.get("monto_asignado", 0) for p in papeles)
    disponible = capital_total - monto_asignado_total
    return render_template(
        "config.html",
        capital_total=capital_total,
        papeles=papeles,
        instrucciones=instrucciones,
        monto_asignado_total=monto_asignado_total,
        disponible=disponible,
    )


@app.route("/papel/<simbolo>")
def detalle_papel(simbolo):
    cfg = db.get_config()
    papel = next((p for p in cfg.get("papeles", []) if p["simbolo"] == simbolo.upper()), None)
    mercado = papel["mercado"] if papel else "bCBA"
    return render_template("stock_detail.html", simbolo=simbolo.upper(), mercado=mercado)


@app.route("/api/historico/<simbolo>")
def api_historico(simbolo):
    mercado = request.args.get("mercado", "bCBA")
    dias = int(request.args.get("dias", 90))
    hasta = datetime.now().strftime("%Y-%m-%d")
    desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")
    try:
        historico = iol_client.get_historico(simbolo, desde, hasta, mercado=mercado)
        puntos = [
            {"fecha": h.get("fechaHora", "")[:10], "cierre": h.get("ultimoPrecio")}
            for h in historico
        ]
        return jsonify({"ok": True, "puntos": puntos})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/historial")
def historial():
    movimientos = db.get_movimientos()
    return render_template("historial.html", movimientos=movimientos)


@app.route("/balances")
def balances_view():
    vista = request.args.get("vista", "mensual")
    if vista not in ("mensual", "anual"):
        vista = "mensual"
    datos = balances.calcular(db.get_movimientos(10000))
    return render_template(
        "balances.html", vista=vista, periodos=balances.agrupar(datos, vista),
        total=balances.total_general(datos), comision=balances.COMISION_PCT,
    )


@app.route("/asistente", methods=["GET", "POST"])
def asistente_view():
    respuesta = None
    mensaje_usuario = None
    if request.method == "POST":
        mensaje_usuario = request.form.get("mensaje", "").strip()
        if mensaje_usuario:
            respuesta = asistente.procesar_mensaje(mensaje_usuario)
    historial = db.get_historial_asistente()
    return render_template(
        "asistente.html", respuesta=respuesta, mensaje_usuario=mensaje_usuario, historial=historial
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
