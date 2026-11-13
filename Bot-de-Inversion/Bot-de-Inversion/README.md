# Bot de trading swing para IOL

Proyecto final · Conrado Maldonado · 2026

Bot escrito en Python que hace **swing trading** (posiciones de días a semanas) con acciones argentinas y CEDEARs en una cuenta de **IOL (InvertirOnline)**. Elige los papeles a seguir, decide las compras con reglas técnicas fijas, usa inteligencia artificial solo para vetar compras por noticias negativas y envía las órdenes de forma automática. Todo lo que hace queda registrado y se puede consultar en un panel web.

---

## Qué hace

De lunes a viernes, entre las 11:00 y las 17:00 (hora de Argentina), el bot repite este circuito:

1. **Screener.** Toma de IOL los 40 papeles más líquidos, mide su volumen y volatilidad y se queda con los 20 más activos.
2. **Radar de noticias.** Una IA (Gemini) lee noticias del mercado y propone hasta 6 papeles con un motivo claro. Los papeles nuevos se validan contra IOL y se siguen durante 5 días.
3. **Filtro técnico.** Cada papel se analiza con medias móviles de 9, 12, 20 y 50 ruedas, MACD, volatilidad y retornos recientes. Entra por tendencia confirmada o por un arranque detectado con el MACD, y se descartan los que ya subieron demasiado.
4. **Veto de la IA.** Para los papeles que pasan el filtro, la IA revisa las noticias y puede frenar la compra si encuentra algo negativo. Las noticias nunca son el motivo para comprar.
5. **Compra.** Calcula el tamaño de la posición según el riesgo, vuelve a leer el precio justo antes de enviar la orden y verifica que la compra figure en la cartera.

Con posiciones abiertas:

- Vigila los stops cada 5 minutos.
- Revisa cada posición a las 11:30, 13:30 y 15:30.
- Hace un escaneo técnico cada 30 minutos para detectar entradas más temprano.

## Reglas de control de riesgo

Las salidas las deciden reglas fijas; la IA no puede saltearlas.

| Regla | Valor |
| --- | --- |
| Stop-loss | Entre 5% y 10% bajo el precio de compra, según la volatilidad del papel (2,5 veces su movimiento diario medio) |
| Tamaño de cada compra | Arriesga como máximo 1% del capital asignado al bot |
| Stop móvil | Se activa al superar +6%; vende si el precio retrocede 4% desde el máximo (3% si el máximo pasó +10%) |
| Objetivo | Al llegar a +12% consulta a la IA si vender o dejar correr |
| Límites | Máximo 6 posiciones abiertas y 2 compras nuevas por día |
| No perseguir | No compra si el papel subió más de 10% en 5 ruedas, 25% en 20 ruedas o está más de 6% sobre su media de 12 |
| Enfriamiento | No vuelve a comprar un papel hasta 5 días después de venderlo |
| Confianza de la IA | Mínimo 6 sobre 10 para comprar; no interviene en las ventas |

## Arquitectura

```
   API de IOL        IA (Gemini / Groq /        Fuentes de
 (precios, cartera,      OpenRouter)              noticias
    órdenes)
        ▲                    ▲                       ▲
        └────────────────────┼───────────────────────┘
                             │
                    [ Motor del bot ]
                      run_bot.py
                             │ guarda
                             ▼
                    [ MongoDB Atlas ]
                             │ lee
                             ▼
                [ Panel web Flask :5000 ]
```

- **Motor del bot:** el único componente que opera. Corre en una PC con Windows que arranca el bot sola al iniciar sesión.
- **MongoDB Atlas:** base de datos en la nube donde se guardan la configuración, las operaciones y los balances.
- **Panel web:** aplicación Flask que solo lee la base. Abrirlo no crea un segundo bot.
- **IA:** Gemini como modelo principal; si falla o se queda sin cuota, pasa a Groq u OpenRouter. Todos con planes gratuitos.

## Panel web

El panel (`http://localhost:5000`) tiene estas secciones:

- **Posiciones:** capital asignado al bot, posiciones abiertas, variación y stop de cada una.
- **Historial:** cada orden con su motivo, incluidas las compras que la IA vetó.
- **Balances:** resultado mes a mes y año a año: qué se compró y vendió, a qué precio y cuánto se ganó o perdió. Se calcula por el método FIFO y descuenta una comisión estimada de 0,67% por lado.
- **Asistente:** chat para cambiar la configuración en lenguaje natural (por ejemplo, "bajá el máximo de posiciones a 4").
- **Configuración:** capital asignado y parámetros de la estrategia.

## Estructura del proyecto

| Archivo | Qué hace |
| --- | --- |
| `run_bot.py` | Punto de entrada: arranca el bot, elige la estrategia y agenda las tareas |
| `swing_engine.py` | Estrategia actual (swing): condiciones de entrada, stops y salidas |
| `live_engine.py` | Estrategia anterior (intradía, cruce de medias 9/12 en velas cortas) |
| `bot_engine.py` | Primera versión del ciclo de cruce de medias 9/12 |
| `screener.py` | Elige los papeles a seguir |
| `market_data.py` | Cálculo de medias móviles cruzando IOL y Yahoo Finance |
| `gemini_decision.py` | Consultas a la IA y cambio de modelo si uno falla |
| `fuentes_noticias.py` | Junta noticias de varios medios |
| `iol_client.py` | Conexión con la API de IOL: cartera, cotizaciones y órdenes |
| `db.py` | Acceso a MongoDB Atlas |
| `balances.py` | Cálculo de resultados mensuales y anuales (FIFO) |
| `app.py` | Panel web en Flask |
| `asistente.py` | Cambios de configuración por chat |
| `templates/`, `static/` | Vistas HTML, estilos y gráficos del panel |
| `diagnostico_iol.py`, `test_ias.py` | Scripts de prueba de la conexión con IOL y con las IAs |
| `iniciar*.bat` | Accesos directos para iniciar el bot y el panel en Windows |
| `requirements.txt` | Dependencias de Python |
| `.env.example` | Variables de entorno necesarias, sin valores |
| `balances_config.example.json` | Formato del archivo opcional de ajustes de balances |

## Robustez

Como opera con dinero real, el bot está diseñado para no actuar cuando hay dudas:

- Si IOL no responde, reintenta la lectura de la cartera; si no puede leerla, no compra ni vende en ese ciclo.
- Valida la respuesta de cada orden: IOL puede aceptarla, rechazarla o dejarla pendiente.
- Si una compra no se confirma en unos 2 minutos, la cancela. Si una venta queda con un precio viejo, la cancela y la reenvía al precio actual.
- No repite la compra de un papel que ya tiene una orden pendiente.
- Si un modelo de IA falla, prueba con el siguiente.

## Tecnologías

Python · Flask · APScheduler · pymongo · requests · MongoDB Atlas · Chart.js · API de IOL · Gemini, Groq y OpenRouter

## Configuración y ejecución

> Requiere una cuenta de IOL con acceso a la API, una base en MongoDB Atlas y claves de las IAs. Las credenciales nunca se incluyen en el repositorio.

1. Instalar Python 3.11 o superior.
2. Crear y activar un entorno virtual:
   ```
   python -m venv venv
   venv\Scripts\activate
   ```
3. Instalar dependencias:
   ```
   pip install -r requirements.txt
   ```
4. Copiar `.env.example` como `.env` y completar los valores.
5. Iniciar el bot y el panel:
   ```
   python run_bot.py
   ```
6. Abrir `http://localhost:5000`.

## Limitaciones y próximos pasos

El bot lleva pocas semanas operando y las reglas todavía no se validaron con datos históricos, por lo que los resultados no alcanzan para concluir si la estrategia gana dinero.

- Opera solo en contado inmediato: en días sin liquidación bancaria las órdenes no se ejecutan.
- Usa gráficos diarios, por lo que detecta los arranques con cierto retraso.
- La IA depende de noticias públicas que pueden estar ya reflejadas en el precio.

Mejoras previstas: backtest con historial de precios, un modo sombra para probar reglas sin operar, confirmación de los CEDEARs con el gráfico del papel en EE.UU. y reintento a 24 hs en días sin contado inmediato.

## Aviso

Proyecto académico. No es una recomendación de inversión. Operar en bolsa implica riesgo de pérdida.

© 2026 Conrado Maldonado. Todos los derechos reservados. No se permite copiar, modificar ni distribuir este código sin autorización del autor.
