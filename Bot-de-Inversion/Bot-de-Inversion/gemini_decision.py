"""
Le pasa a Gemini (tier gratuito) el snapshot técnico de un papel, MÁS contexto
de noticias financieras reales (Ámbito Financiero, Rava Bursátil, Yahoo
Finance — ver fuentes_noticias.py), y le pide una decisión concreta:
COMPRAR, VENDER o MANTENER, con una breve razón.
"""
import os
import json
import time
from google import genai
from google.genai import types
from dotenv import load_dotenv

import db
import fuentes_noticias

load_dotenv()
# Timeout de 60 s por pedido: sin esto, una llamada colgada frena TODO el bot (pasó el 23/10: 3 horas sin vigilar).
_cliente = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"), http_options=types.HttpOptions(timeout=60_000))

MODELO = "gemini-3.8-flash"  # rápido y gratis (reemplaza al gemini-1.5-flash, discontinuado por Google)

PROMPT_BASE = """Sos un asistente de análisis de inversión de corto plazo para el
mercado argentino. Te paso datos técnicos (medias móviles de 9 y 12 ruedas,
cruzadas entre dos fuentes) Y contexto de noticias financieras reales y
recientes. Combiná ambas cosas para decidir una sola acción concreta —no te
bases solo en el técnico si las noticias contradicen claramente la señal.

IMPORTANTE: esto es TRADING activo, no inversión de largo plazo. La estrategia
es por cruce de medias 9/12: se compra con señal alcista y se vende con señal
bajista. No propongas COMPRAR con señal bajista ni VENDER con señal alcista; el
sistema lo bloquea igual. Tu trabajo es confirmar la señal o frenarla (MANTENER)
si las noticias o las instrucciones del usuario la contradicen claramente.

Datos técnicos del papel {simbolo}:
{datos}

Contexto de noticias/mercado sobre este papel:
{contexto_noticias}

Tenencia actual: {cantidad} unidades a un precio promedio de {precio_compra}.
Precio actual: {precio_actual}.

Instrucciones permanentes del usuario (respetalas siempre que apliquen a esta decisión):
{instrucciones}

Respondé SOLO con un JSON, sin texto adicional ni markdown, con este formato exacto:
{{"accion": "COMPRAR" | "VENDER" | "MANTENER", "razon": "una frase corta que mencione el técnico, la noticia si aplica, y si alguna instrucción del usuario influyó en la decisión"}}
"""


# ---- Cadena de IAs (todas gratuitas) -------------------------------------------------
# 1) Gemini (3 modelos; el plan gratuito da pocos pedidos por día POR MODELO).
# 2) Groq y OpenRouter, si hay clave en el .env (GROQ_API_KEY / OPENROUTER_API_KEY).
#    Hablan el protocolo OpenAI, así que se llaman con requests. Se usan solo si las
#    anteriores fallan o están sin cupo. Para sumar otra (ej. Cerebras) basta una línea
#    más en PROVEEDORES_OPENAI.
MODELOS_RESPALDO = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]
PROVEEDORES_OPENAI = [
    # (nombre, variable de entorno con la clave, URL base, modelo)
    ("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "google/gemma-4-31b-it:free"),
]
SEGUNDOS_ENTRE_LLAMADAS = 13
RONDAS = 2  # cuántas veces se recorre la cadena completa
_ultima_llamada = {}
_sin_cupo = {}   # nombre -> hora hasta la cual se saltea (cupo diario agotado)


class _Respuesta:
    """Mismo formato que las respuestas de Gemini: el resto del código usa .text"""
    def __init__(self, text):
        self.text = text


def _llamar_openai(nombre, clave, base, modelo, prompt):
    import requests
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {clave}"},
        json={"model": modelo, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2},
        timeout=40,
    )
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} {nombre}: {r.text[:200]}")
    return _Respuesta(r.json()["choices"][0]["message"]["content"])


def _cadena():
    cadena = [("gemini", m, None) for m in [MODELO] + MODELOS_RESPALDO]
    for nombre, var, base, modelo in PROVEEDORES_OPENAI:
        clave = os.environ.get(var)
        if clave:
            cadena.append((nombre, modelo, (clave, base)))
    return cadena


def _generar(prompt, rondas=RONDAS):
    ultimo_error = None
    for ronda in range(rondas):
        for proveedor, modelo, extra in _cadena():
            clave_cupo = f"{proveedor}:{modelo}"
            if time.time() < _sin_cupo.get(clave_cupo, 0):
                continue
            espera = SEGUNDOS_ENTRE_LLAMADAS - (time.time() - _ultima_llamada.get(clave_cupo, 0))
            if espera > 0:
                time.sleep(espera)
            _ultima_llamada[clave_cupo] = time.time()
            try:
                if proveedor == "gemini":
                    respuesta = _cliente.models.generate_content(model=modelo, contents=prompt)
                else:
                    respuesta = _llamar_openai(proveedor, extra[0], extra[1], modelo, prompt)
                if (proveedor, modelo) != ("gemini", MODELO):
                    print(f"  (respondió {proveedor}:{modelo})", flush=True)
                return respuesta
            except Exception as e:
                ultimo_error = e
                msg = str(e)
                if "PerDay" in msg or "per day" in msg.lower():
                    _sin_cupo[clave_cupo] = time.time() + 3600   # cupo diario agotado: no insistir por 1 h
                # Cualquier falla de un proveedor (504 DEADLINE_EXCEEDED, timeout, conexión, 4xx/5xx...)
                # pasa al siguiente de la cadena: es el plan B. Solo si fallan TODOS se rinde.
                print(f"  ({proveedor}:{modelo} falló: {msg[:120]}) -> pruebo el siguiente", flush=True)
        if ronda < rondas - 1:
            time.sleep(20)
    raise ultimo_error


def decidir(simbolo, datos_medias, cantidad=0, precio_compra=0, precio_actual=0, ticker_yf=None):
    contexto_noticias = fuentes_noticias.contexto_para_papel(simbolo, ticker_yf)
    instrucciones = db.get_instrucciones()
    instrucciones_texto = (
        "\n".join(f"- {i}" for i in instrucciones) if instrucciones else "(el usuario no dejó instrucciones especiales)"
    )

    prompt = PROMPT_BASE.format(
        simbolo=simbolo,
        datos=json.dumps(datos_medias, ensure_ascii=False),
        contexto_noticias=contexto_noticias,
        cantidad=cantidad,
        precio_compra=precio_compra,
        precio_actual=precio_actual,
        instrucciones=instrucciones_texto,
    )
    try:
        respuesta = _generar(prompt)
        texto = respuesta.text.strip().strip("`").replace("json", "", 1).strip()
        data = json.loads(texto)
        if data.get("accion") not in ("COMPRAR", "VENDER", "MANTENER"):
            data["accion"] = "MANTENER"
        return data
    except Exception as e:
        # Si Gemini falla o devuelve algo raro, no se opera: mejor no hacer
        # nada que ejecutar una orden mal interpretada.
        return {"accion": "MANTENER", "razon": f"error consultando Gemini: {e}"}
