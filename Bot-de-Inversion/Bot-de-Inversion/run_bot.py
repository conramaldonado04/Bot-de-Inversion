"""
Proceso independiente del dashboard web (APScheduler). Dos estrategias:

  ESTRATEGIA = "swing"     mediano plazo (días/semanas): una revisión diaria con
                           noticias + tendencia + técnico, y reglas duras de stop
                           y objetivo vigiladas cada 5 min. (ver swing_engine.py)
  ESTRATEGIA = "intradia"  trading en vivo por cruce 9/12 de barras de 5 min
                           (ver live_engine.py), con radar cada 10 min y tick de 30 s.

Para cambiar de estrategia, editar ESTRATEGIA y reiniciar este proceso.
"""
import os
import sys
import traceback
from datetime import datetime

# --- Log a archivo: todo lo que imprime el bot se copia a bot.log (además de la consola) ---
_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG = os.path.join(_DIR, "bot.log")


class _Tee:
    def __init__(self, original, archivo):
        self._o, self._a = original, archivo

    def write(self, texto):
        try:
            if self._o:
                self._o.write(texto)
        except Exception:
            pass
        try:
            self._a.write(texto)
            self._a.flush()
        except Exception:
            pass
        return len(texto)

    def flush(self):
        for f in (self._o, self._a):
            try:
                if f:
                    f.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def _activar_log():
    try:
        if os.path.exists(_LOG) and os.path.getsize(_LOG) > 2_000_000:
            os.replace(_LOG, _LOG + ".old")  # rotación simple
        archivo = open(_LOG, "a", encoding="utf-8", errors="replace")
        sys.stdout = _Tee(sys.stdout, archivo)
        sys.stderr = _Tee(sys.stderr, archivo)
        print(f"\n===== Inicio del bot {datetime.now():%Y-%m-%d %H:%M:%S} =====", flush=True)
    except Exception:
        pass  # si no se puede escribir el log, el bot sigue igual


_activar_log()

from apscheduler.schedulers.blocking import BlockingScheduler

ESTRATEGIA = "swing"

if ESTRATEGIA == "swing":
    import swing_engine as motor
    INTERVALO_SEG = 300
else:
    import live_engine as motor
    INTERVALO_SEG = 30
MINUTOS_RADAR = 10


def radar_seguro():
    try:
        motor.radar()
    except Exception:
        print("ERROR en el radar (sigue vivo y reintenta):", flush=True)
        traceback.print_exc()


def tick_seguro():
    try:
        motor.tick()
    except Exception:
        print(f"[{datetime.now():%H:%M:%S}] ERROR en el tick (sigue vivo y reintenta):", flush=True)
        traceback.print_exc()


if __name__ == "__main__":
    scheduler = BlockingScheduler()
    # OJO: no usar next_run_time=None, en APScheduler crea el job PAUSADO.
    scheduler.add_job(tick_seguro, "interval", seconds=INTERVALO_SEG,
                      max_instances=1, coalesce=True, misfire_grace_time=60)
    if ESTRATEGIA != "swing":
        scheduler.add_job(radar_seguro, "interval", minutes=MINUTOS_RADAR,
                          max_instances=1, coalesce=True, misfire_grace_time=300)

    print(f"Bot arrancado. Estrategia: {ESTRATEGIA.upper()}. Vigilancia cada {INTERVALO_SEG} s. Ctrl+C para parar.", flush=True)
    if ESTRATEGIA != "swing":
        radar_seguro()  # arma el watchlist apenas arranca
    tick_seguro()       # primera pasada inmediata (en swing: si ya pasó la hora, hace la revisión diaria)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
