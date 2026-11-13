"""
Script de diagnóstico puntual: prueba login + histórico contra la API real
de IOL y muestra el error completo si algo falla. Ejecutar con:
    python diagnostico_iol.py
No modifica nada, solo lee tu .env y hace 2 requests de prueba.
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

BASE = "https://api.invertironline.com"
username = os.environ.get("IOL_USERNAME")
password = os.environ.get("IOL_PASSWORD")

print(f"Usuario leído del .env: {username!r}")
print(f"Longitud del password leído: {len(password) if password else 0} caracteres")
print("-" * 50)

resp = requests.post(
    f"{BASE}/token",
    data={"username": username, "password": password, "grant_type": "password"},
    timeout=20,
)
print(f"LOGIN -> status {resp.status_code}")
if resp.status_code != 200:
    print("LOGIN body:", resp.text[:1000])
    print("\n>>> El login falló, revisá usuario/clave en el .env (no debe tener")
    print(">>> comillas ni espacios de más alrededor del '=').")
else:
    token = resp.json()["access_token"]
    print("Login OK.")
    print("-" * 50)

    url = f"{BASE}/api/v2/bCBA/Titulos/GGAL/Cotizacion/seriehistorica/2026-06-12/2026-09-10/sinAjustar"
    print(f"Probando: GET {url}")
    r2 = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    print(f"HISTORICO -> status {r2.status_code}")
    print("HISTORICO body (primeros 1500 caracteres):")
    print(r2.text[:1500])
