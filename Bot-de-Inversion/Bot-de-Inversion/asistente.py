"""
Asistente conversacional del panel: le escribís en lenguaje natural qué
querés cambiar (montos, papeles seguidos, instrucciones permanentes) o
pedirle que busque nuevas opciones de inversión, y Gemini interpreta la
intención, la convierte a una acción concreta, y te devuelve una respuesta
en texto confirmando qué hizo. Cada ida y vuelta queda guardada en Mongo
para poder ver el historial completo en el panel.

Para "buscar opciones de inversión", Gemini NO inventa tickers de memoria:
se le pasa contexto real de noticias financieras (Ámbito Financiero, Rava
Bursátil, Yahoo Finance — ver fuentes_noticias.py) más el resultado del
screener cuantitativo (volumen/volatilidad real de mercado), y recién con
esos datos concretos arma su recomendación.

Instrucciones permanentes: cosas como "no vendas las tenencias actuales,
operá solo con capital nuevo" se guardan en config.instrucciones y se le
pasan a Gemini en CADA decisión de compra/venta (ver gemini_decision.py)
para que las respete. Ojo: esto es un pedido a nivel de prompt, no una regla
de código dura — Gemini normalmente lo va a respetar, pero no hay una
verificación adicional en bot_engine.py que lo garantice al 100%.
"""
import os
import json
from google import genai
from dotenv import load_dotenv

import db
import screener
import fuentes_noticias
import gemini_decision

load_dotenv()
_cliente = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
MODELO = "gemini-3.8-flash"  # reemplaza al gemini-1.5-flash, discontinuado por Google

ACCIONES_VALIDAS = {
    "cambiar_capital_total",
    "cambiar_monto_papel",
    "agregar_papel",
    "quitar_papel",
    "agregar_instruccion",
    "quitar_instruccion",
    "buscar_opciones",
    "ninguna",
}

PROMPT_BASE = """Sos el asistente de un panel de trading. El usuario te escribe en
lenguaje natural qué quiere cambiar en su configuración, o te da una
instrucción permanente para que el bot la respete de acá en adelante. Tu
trabajo es interpretar la intención y devolver SIEMPRE un JSON, sin texto
adicional ni markdown, con este formato exacto:

{{"accion": "<una de: cambiar_capital_total, cambiar_monto_papel, agregar_papel, quitar_papel, agregar_instruccion, quitar_instruccion, buscar_opciones, ninguna>",
  "parametros": {{}},
  "respuesta": "una frase corta confirmándole al usuario qué vas a hacer, en español"}}

Reglas de "parametros" según la acción:
- cambiar_capital_total: {{"monto": <numero>}}
- cambiar_monto_papel: {{"simbolo": "<TICKER>", "monto": <numero>}}
- agregar_papel: {{"simbolo": "<TICKER>"}}
- quitar_papel: {{"simbolo": "<TICKER>"}}
- agregar_instruccion: {{"texto": "<la instrucción reformulada de forma clara y corta, en español>"}}
  Usá esta acción cuando el usuario te pida algo que el bot debe respetar de
  forma permanente en sus decisiones de compra/venta, por ejemplo "no vendas
  las tenencias que ya tengo", "priorizá CEDEARs sobre acciones argentinas",
  "no operes los viernes". NO uses esto para pedidos de una sola vez.
- quitar_instruccion: {{"texto": "<el texto EXACTO de una instrucción existente, tal cual figura en la lista actual>"}}
- buscar_opciones: {{}} (no inventes tickers vos, esto dispara un screener real con datos de mercado + noticias)
- ninguna: {{}} (usalo si el usuario solo pregunta algo informativo, y contestale en "respuesta")

Nunca inventes datos de mercado ni tickers en tu "respuesta" para la acción
"ninguna" si no estás seguro — si te preguntan por precios o performance
actual, aclarales que para eso necesitan mirar el panel de Tenencias.

Configuración actual del usuario:
{config}

Mensaje del usuario: "{mensaje}"
"""

PROMPT_OPCIONES = """Sos un asistente de inversión para el mercado argentino. Te paso
el resultado de un screener cuantitativo real (papeles con mayor volumen y
volatilidad de los últimos 30 días) y los titulares financieros más
recientes. Con esos dos insumos, elegí hasta 5 papeles que te parezcan las
mejores opciones a considerar ahora mismo, y para cada uno escribí una razón
de una línea basada en lo que ves en los datos, no en conocimiento previo
tuyo.

Ranking del screener (volumen/volatilidad real, ya ordenado de mejor a peor):
{ranking}

Titulares financieros recientes (Ámbito Financiero):
{noticias}

Respondé en español, en un texto corto y legible (no JSON), tipo lista, que
el usuario va a leer directo en el panel.
"""


def procesar_mensaje(mensaje: str) -> str:
    respuesta = _procesar_mensaje(mensaje)
    try:
        db.guardar_interaccion_asistente(mensaje, respuesta)
    except Exception:
        pass  # que falle el guardado del historial nunca debe romper la respuesta
    return respuesta


def _procesar_mensaje(mensaje: str) -> str:
    cfg = db.get_config()
    config_resumen = {
        "capital_total": cfg.get("capital_total", 0),
        "papeles": [
            {"simbolo": p["simbolo"], "monto_asignado": p.get("monto_asignado", 0)}
            for p in cfg.get("papeles", [])
        ],
        "instrucciones_vigentes": cfg.get("instrucciones", []),
    }

    prompt = PROMPT_BASE.format(
        config=json.dumps(config_resumen, ensure_ascii=False),
        mensaje=mensaje,
    )

    try:
        respuesta_ia = gemini_decision._generar(prompt, rondas=1)
        texto = respuesta_ia.text.strip().strip("`").replace("json", "", 1).strip()
        data = json.loads(texto)
    except Exception as e:
        if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
            return ("Gemini agotó el cupo gratuito de pedidos (en todos los modelos que probé). "
                    "Se renueva solo; volvé a intentar en un rato. Mientras tanto podés cambiar el capital "
                    "y los montos directo desde Configuración.")
        if "503" in str(e) or "UNAVAILABLE" in str(e):
            return "Gemini está saturado en este momento (probé también con los modelos de respaldo). Probá de nuevo en unos minutos."
        return f"No pude interpretar bien el pedido (error: {e}). ¿Podés reformularlo?"

    accion = data.get("accion", "ninguna")
    parametros = data.get("parametros", {})
    respuesta = data.get("respuesta", "Listo.")

    if accion not in ACCIONES_VALIDAS:
        return respuesta

    try:
        if accion == "cambiar_capital_total":
            db.set_capital_total(float(parametros["monto"]))

        elif accion == "cambiar_monto_papel":
            db.set_monto_papel(parametros["simbolo"].upper(), float(parametros["monto"]))

        elif accion == "agregar_papel":
            db.agregar_papel(parametros["simbolo"].upper())

        elif accion == "quitar_papel":
            db.quitar_papel(parametros["simbolo"].upper())

        elif accion == "agregar_instruccion":
            db.agregar_instruccion(parametros["texto"])

        elif accion == "quitar_instruccion":
            db.quitar_instruccion(parametros["texto"])

        elif accion == "buscar_opciones":
            respuesta = _buscar_opciones_con_contexto()

    except Exception as e:
        return f"Entendí el pedido pero falló al aplicarlo: {e}"

    return respuesta


def _buscar_opciones_con_contexto() -> str:
    """
    Corre el screener real (datos de mercado) + trae noticias reales, y le
    pide a Gemini que arme una recomendación EXPLICADA en base a esos dos
    insumos concretos (no le pide que "sepa" nada de memoria).
    """
    ranking = screener.ejecutar_screener()
    noticias = fuentes_noticias.contexto_general_mercado()

    prompt = PROMPT_OPCIONES.format(
        ranking=", ".join(ranking) if ranking else "sin datos disponibles",
        noticias=noticias,
    )

    try:
        respuesta_ia = gemini_decision._generar(prompt, rondas=1)
        texto_final = respuesta_ia.text.strip()
    except Exception as e:
        return f"Actualicé el watchlist con datos de mercado ({', '.join(ranking)}), pero no pude generar el análisis con Gemini: {e}"

    return f"Watchlist actualizado con datos reales de mercado.\n\n{texto_final}"
