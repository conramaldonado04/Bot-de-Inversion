"""
Fuentes de información financiera real (sin costo, sin API key) para que
Gemini razone con datos actuales en vez de solo indicadores técnicos.

Fuentes usadas:
- Ámbito Financiero (RSS oficial, sección Finanzas/Mercados): titulares del
  día sobre economía argentina en general.
- El Cronista (sección Finanzas y Mercados, scraping de titulares): otro
  medio económico argentino de referencia, para no depender de una sola
  fuente local.
- Bloomberg Línea Argentina (sección Argentina, scraping de titulares):
  cobertura de mercados/macro argentina con mirada internacional.
- Investing.com (sección Stock Market News, scraping de titulares): contexto
  global de mercados, útil sobre todo para CEDEARs de empresas de EEUU.
- Rava Bursátil (rava.com/perfil/<ticker>): resumen con IA + menciones
  recientes por papel puntual, generado por el propio programa "La mañana
  del mercado" que ya seguís.
- Yahoo Finance (vía yfinance, ya instalado): noticias recientes en inglés
  para CEDEARs/tickers de EEUU, más el histórico de precios que ya se usa
  para cruzar las medias 9/12.

Todas estas fuentes se acceden vía scraping de páginas públicas (no tienen
API gratuita) o RSS oficial, y van envueltas en try/except: si una página
cambia de diseño o no responde, esa fuente puntual devuelve vacío en vez de
romper el resto del bot. Nunca se inventa contenido si una fuente falla.

Nota sobre la propia IOL: su API pública (developers.invertironline.com) no
tiene ningún endpoint de noticias — solo cotizaciones, cuenta y operar. Por
eso IOL ya se usa al máximo como fuente de PRECIOS (histórico para las medias
9/12, portafolio, cotización), pero no aporta texto/noticias adicional.
"""
import re
import requests
import xml.etree.ElementTree as ET
import yfinance as yf

TIMEOUT = 12
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PanelTradingBot/1.0)"}

RSS_AMBITO_FINANZAS = "https://www.ambito.com/rss/pages/finanzas.xml"
RSS_AMBITO_MERCADOS = "https://www.ambito.com/rss/pages/mercados.xml"

URL_CRONISTA_FINANZAS = "https://www.cronista.com/finanzas-mercados/"
URL_BLOOMBERG_LINEA_ARG = "https://www.bloomberglinea.com/latinoamerica/argentina/"
URL_INVESTING_STOCK_NEWS = "https://www.investing.com/news/stock-market-news"

RSS_CNBC_MERCADOS = "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147"
RSS_MARKETWATCH = "https://feeds.content.dowjones.io/public/rss/mw_topstories"


def _parsear_rss(url, limite=8):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = []
        for item in root.findall(".//item")[:limite]:
            titulo = item.findtext("title", "").strip()
            fecha = item.findtext("pubDate", "").strip()
            if titulo:
                items.append({"titulo": titulo, "fecha": fecha})
        return items
    except Exception:
        return []


def _extraer_titulares_html(url, tags=("h1", "h2", "h3"), limite=8):
    """
    Extractor best-effort de titulares para páginas sin RSS: busca texto
    dentro de tags de encabezado y se queda con lo que parece un título de
    nota (largo razonable, sin duplicados). Si el sitio cambia de diseño,
    en el peor caso devuelve una lista vacía o con ruido leve, nunca rompe.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        html = resp.text
        html = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
        html = re.sub(r"<style.*?</style>", " ", html, flags=re.S)

        titulares = []
        for tag in tags:
            for m in re.finditer(rf"<{tag}[^>]*>(.*?)</{tag}>", html, flags=re.S):
                texto = re.sub(r"<[^>]+>", " ", m.group(1))
                texto = re.sub(r"\s+", " ", texto).strip()
                if 15 <= len(texto) <= 160 and texto not in titulares:
                    titulares.append(texto)
                if len(titulares) >= limite:
                    break
            if len(titulares) >= limite:
                break
        return titulares[:limite]
    except Exception:
        return []


def noticias_generales_argentina(limite=6):
    """Titulares generales de economía/mercados de Ámbito Financiero (RSS)."""
    items = _parsear_rss(RSS_AMBITO_FINANZAS, limite) + _parsear_rss(RSS_AMBITO_MERCADOS, limite)
    return items[:limite]


def noticias_cronista(limite=6):
    """Titulares de la sección Finanzas y Mercados de El Cronista."""
    return _extraer_titulares_html(URL_CRONISTA_FINANZAS, limite=limite)


def noticias_bloomberg_linea(limite=6):
    """Titulares de la sección Argentina de Bloomberg Línea."""
    return _extraer_titulares_html(URL_BLOOMBERG_LINEA_ARG, limite=limite)


def noticias_cnbc_mercados(limite=5):
    """Titulares de mercados de CNBC (RSS oficial)."""
    return [i["titulo"] for i in _parsear_rss(RSS_CNBC_MERCADOS, limite)]


def noticias_marketwatch(limite=5):
    """Titulares principales de MarketWatch (RSS oficial)."""
    return [i["titulo"] for i in _parsear_rss(RSS_MARKETWATCH, limite)]


def noticias_investing_global(limite=6):
    """Titulares globales de mercado de Investing.com (contexto para CEDEARs)."""
    return _extraer_titulares_html(URL_INVESTING_STOCK_NEWS, limite=limite)


def resumen_rava(simbolo: str):
    """
    Resumen con IA + menciones recientes de rava.com/perfil/<simbolo>.
    Devuelve None si no se pudo extraer nada (papel no cubierto, cambio de
    diseño de la página, etc.) — nunca lanza excepción hacia afuera.
    """
    try:
        url = f"https://www.rava.com/perfil/{simbolo.upper()}"
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        html = resp.text

        # Extracción best-effort: busca el bloque de resumen de IA por texto
        # plano, sin depender de clases CSS específicas (cambian seguido).
        texto = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
        texto = re.sub(r"<[^>]+>", " ", texto)
        texto = re.sub(r"\s+", " ", texto).strip()

        idx = texto.lower().find("resumen")
        fragmento = texto[idx: idx + 600] if idx != -1 else None
        return fragmento
    except Exception:
        return None


def noticias_google(consulta: str, limite=5, idioma="en-US", pais="US"):
    """Titulares recientes de Google News (RSS público, sin API key) para una consulta."""
    try:
        resp = requests.get(
            "https://news.google.com/rss/search",
            params={"q": f"{consulta} when:7d", "hl": idioma, "gl": pais, "ceid": f"{pais}:{idioma.split('-')[0]}"},
            headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = []
        for item in root.findall(".//item")[:limite]:
            titulo = item.findtext("title", "").strip()
            if titulo:
                items.append({"titulo": titulo})
        return items
    except Exception:
        return []


def noticias_yfinance(simbolo: str, ticker_yf: str, limite=5):
    """
    Noticias recientes de un papel. Primero Yahoo Finance; si falla o viene
    vacío (pasa seguido: "Failed to retrieve the news"), cae a Google News.
    """
    try:
        noticias = yf.Ticker(ticker_yf).news or []
        resultado = []
        for n in noticias[:limite]:
            titulo = n.get("title") or n.get("content", {}).get("title")
            if titulo:
                resultado.append({"titulo": titulo})
        if resultado:
            return resultado
    except Exception:
        pass
    return noticias_google(f"{ticker_yf} stock", limite)


def contexto_general_mercado() -> str:
    """
    Bloque de titulares generales de la city porteña + mercados globales,
    combinando Ámbito Financiero, El Cronista, Bloomberg Línea e
    Investing.com. Se usa para el asistente ("buscá nuevas opciones") y como
    contexto macro dentro de cada decisión de compra/venta.
    """
    bloques = []

    ambito = noticias_generales_argentina()
    if ambito:
        bloques.append("Ámbito Financiero:\n" + "\n".join(f"- {i['titulo']}" for i in ambito))

    cronista = noticias_cronista()
    if cronista:
        bloques.append("El Cronista:\n" + "\n".join(f"- {t}" for t in cronista))

    bloomberg = noticias_bloomberg_linea()
    if bloomberg:
        bloques.append("Bloomberg Línea (Argentina):\n" + "\n".join(f"- {t}" for t in bloomberg))

    investing = noticias_investing_global()
    if investing:
        bloques.append("Investing.com (mercados globales):\n" + "\n".join(f"- {t}" for t in investing))

    if not bloques:
        return "Sin titulares disponibles en este momento."
    return "\n\n".join(bloques)


def contexto_para_papel(simbolo: str, ticker_yf: str = None) -> str:
    """
    Arma un bloque de texto con todo el contexto disponible para un papel,
    listo para el prompt de Gemini. Combina muchas fuentes (cada una en
    try/except; si una falla se omite):
      - Macro Argentina: Ámbito, El Cronista, Bloomberg Línea.
      - Específico del papel: Rava Bursátil, Yahoo Finance, Google News
        (en inglés y en español).
      - Mercado global: Investing.com, CNBC, MarketWatch.
    Imprime en el log qué fuentes devolvieron datos, para poder diagnosticar.
    """
    partes = []
    fuentes_ok = []

    ambito = noticias_generales_argentina(limite=3)
    cronista = noticias_cronista(limite=2)
    bloomberg = noticias_bloomberg_linea(limite=2)
    macro = []
    for nombre, items in (("Ámbito", [m["titulo"] for m in ambito]), ("Cronista", cronista), ("Bloomberg Línea", bloomberg)):
        if items:
            macro += items
            fuentes_ok.append(nombre)
    if macro:
        partes.append("Contexto macro Argentina (Ámbito/Cronista/Bloomberg Línea): " + "; ".join(macro[:6]))

    rava = resumen_rava(simbolo)
    if rava:
        fuentes_ok.append("Rava")
        partes.append(f"Resumen Rava Bursátil ({simbolo}): {rava}")

    if ticker_yf:
        try:
            yfn = yf.Ticker(ticker_yf).news or []
        except Exception:
            yfn = []
        titulos_yf = []
        for n in yfn[:5]:
            t = n.get("title") or n.get("content", {}).get("title")
            if t:
                titulos_yf.append(t)
        if titulos_yf:
            fuentes_ok.append("Yahoo")
            partes.append("Noticias recientes (Yahoo Finance): " + "; ".join(titulos_yf))

        g_en = noticias_google(f"{ticker_yf} stock", 5)
        if g_en:
            fuentes_ok.append("GoogleNews-EN")
            partes.append("Noticias recientes (Google News): " + "; ".join(n["titulo"] for n in g_en))

    g_es = noticias_google(f"{simbolo} acciones", 4, idioma="es-419", pais="AR")
    if g_es:
        fuentes_ok.append("GoogleNews-ES")
        partes.append("Noticias en español (Google News): " + "; ".join(n["titulo"] for n in g_es))

    globales = []
    for nombre, f in (("Investing", lambda: noticias_investing_global(limite=2)),
                      ("CNBC", lambda: noticias_cnbc_mercados(2)),
                      ("MarketWatch", lambda: noticias_marketwatch(2))):
        try:
            items = f()
        except Exception:
            items = []
        if items:
            globales += items
            fuentes_ok.append(nombre)
    if globales:
        partes.append("Contexto mercado global (Investing/CNBC/MarketWatch): " + "; ".join(globales[:6]))

    print(f"[noticias {simbolo}] fuentes con datos: {', '.join(fuentes_ok) or 'ninguna'}", flush=True)
    if not partes:
        return "Sin contexto de noticias disponible para este papel."
    return "\n".join(partes)
