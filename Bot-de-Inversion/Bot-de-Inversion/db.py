"""
Conexión a MongoDB Atlas y helpers de acceso a datos.

Colecciones usadas:
- config       -> un único documento con el capital total y la lista de papeles seguidos
- movimientos  -> historial de compras/ventas (lo va a ir llenando el bot más adelante;
                  por ahora se puede cargar a mano o vía IOL si el broker lo expone)
"""
import os
from datetime import datetime
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

_client = None
_db = None

# Papeles con los que habíamos armado la cartera activa del bot
PAPELES_INICIALES = [
    {"simbolo": "GGAL", "mercado": "bCBA", "monto_asignado": 0},
    {"simbolo": "YPFD", "mercado": "bCBA", "monto_asignado": 0},
    {"simbolo": "PAMP", "mercado": "bCBA", "monto_asignado": 0},
    {"simbolo": "BMA",  "mercado": "bCBA", "monto_asignado": 0},
    {"simbolo": "VIST", "mercado": "bCBA", "monto_asignado": 0},
    {"simbolo": "TSLA", "mercado": "bCBA", "monto_asignado": 0},
    {"simbolo": "NVDA", "mercado": "bCBA", "monto_asignado": 0},
    {"simbolo": "MELI", "mercado": "bCBA", "monto_asignado": 0},
    {"simbolo": "PLTR", "mercado": "bCBA", "monto_asignado": 0},
    {"simbolo": "AMD",  "mercado": "bCBA", "monto_asignado": 0},
]


def get_db():
    global _client, _db
    if _db is None:
        uri = os.environ.get("MONGODB_URI")
        if not uri:
            raise RuntimeError(
                "Falta MONGODB_URI en el archivo .env. Copiá .env.example a .env y completalo."
            )
        _client = MongoClient(uri)
        _db = _client.get_database("trading_bot")
        _ensure_config()
    return _db


def _ensure_config():
    """Crea el documento de configuración inicial si todavía no existe."""
    config_col = _db.config
    if config_col.count_documents({}) == 0:
        config_col.insert_one({
            "_id": "main",
            "capital_total": 0,
            "papeles": PAPELES_INICIALES,
        })


def get_config():
    return get_db().config.find_one({"_id": "main"})


def set_capital_total(monto: float):
    get_db().config.update_one({"_id": "main"}, {"$set": {"capital_total": monto}})


def set_monto_papel(simbolo: str, monto: float):
    get_db().config.update_one(
        {"_id": "main", "papeles.simbolo": simbolo},
        {"$set": {"papeles.$.monto_asignado": monto}},
    )


def agregar_papel(simbolo: str, mercado: str = "bCBA"):
    simbolo = simbolo.upper().strip()
    db = get_db()
    existe = db.config.find_one({"_id": "main", "papeles.simbolo": simbolo})
    if existe:
        return False
    db.config.update_one(
        {"_id": "main"},
        {"$push": {"papeles": {"simbolo": simbolo, "mercado": mercado, "monto_asignado": 0}}},
    )
    return True


def quitar_papel(simbolo: str):
    get_db().config.update_one(
        {"_id": "main"}, {"$pull": {"papeles": {"simbolo": simbolo.upper().strip()}}}
    )

def monto_operable(simbolo: str) -> float:
    """
    Si le asignaste monto propio a este papel, se usa ese.
    Si lo dejaste en 0, se reparte en partes iguales el capital total que
    todavía no está asignado a ningún papel, entre todos los papeles que
    también están en 0.
    """
    cfg = get_config()
    capital_total = cfg.get("capital_total", 0)
    papeles = cfg.get("papeles", [])

    asignado_manual = sum(p.get("monto_asignado", 0) for p in papeles)
    en_cero = [p["simbolo"] for p in papeles if p.get("monto_asignado", 0) == 0]

    for p in papeles:
        if p["simbolo"] == simbolo:
            if p.get("monto_asignado", 0) > 0:
                return p["monto_asignado"]
            if not en_cero:
                return 0
            disponible = max(capital_total - asignado_manual, 0)
            return disponible / len(en_cero)
    return 0

def get_movimientos(limit: int = 100):
    return list(
        get_db().movimientos.find({}, {"_id": 0}).sort("fecha", -1).limit(limit)
    )


def registrar_movimiento(mov: dict):
    """mov: {fecha, simbolo, tipo ('compra'/'venta'), cantidad, precio, importe}"""
    get_db().movimientos.insert_one(mov)


def get_instrucciones():
    """
    Instrucciones permanentes que el usuario le fue dando al bot en lenguaje
    natural (ej: "no vendas las tenencias actuales, operá solo con capital
    nuevo"). Se muestran en Configuración y se le pasan a Gemini en cada
    decisión de compra/venta para que las tenga en cuenta.
    """
    return get_config().get("instrucciones", [])


def agregar_instruccion(texto: str):
    texto = texto.strip()
    get_db().config.update_one({"_id": "main"}, {"$push": {"instrucciones": texto}})


def quitar_instruccion(texto: str):
    get_db().config.update_one({"_id": "main"}, {"$pull": {"instrucciones": texto}})


def guardar_interaccion_asistente(mensaje: str, respuesta: str):
    """Guarda cada ida y vuelta con el Asistente para poder ver el historial."""
    get_db().asistente_historial.insert_one({
        "fecha": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "mensaje": mensaje,
        "respuesta": respuesta,
    })


def get_historial_asistente(limit: int = 50):
    """Devuelve el historial de chat del Asistente, del más viejo al más nuevo."""
    items = list(
        get_db().asistente_historial.find({}, {"_id": 0}).sort("$natural", -1).limit(limit)
    )
    return list(reversed(items))
