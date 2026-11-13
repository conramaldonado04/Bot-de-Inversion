import os
import gemini_decision as g
print("=== TEST DE IAs ===", flush=True)
for nombre, var, base, modelo in g.PROVEEDORES_OPENAI:
    clave = os.environ.get(var)
    if not clave:
        print(f"{nombre}: SIN CLAVE", flush=True); continue
    try:
        r = g._llamar_openai(nombre, clave, base, modelo, 'Respondé SOLO este JSON: {"accion":"MANTENER","razon":"test"}')
        print(f"{nombre} {modelo}: OK -> {(r.text or '').strip()[:80]}", flush=True)
    except Exception as e:
        print(f"{nombre} {modelo}: FALLA -> {str(e)[:200]}", flush=True)
print("=== FIN ===", flush=True)
