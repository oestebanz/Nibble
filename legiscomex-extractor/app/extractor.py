"""Extraccion generica de una pagina de Legiscomex ya autenticada.

Devuelve texto, tablas, enlaces y documentos de la pagina (incluidos sus iframes),
aplica el filtro por termino, sigue la paginacion y descarga documentos si se pide.
"""
import re
import unicodedata
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout

from . import config
from .navegador import Navegador, url_permitida


class AccesoDenegado(Exception):
    pass


JS_EXTRAER = r"""
() => {
  const limpio = s => (s || '').replace(/\s+/g, ' ').trim();
  const tablas = [...document.querySelectorAll('table')]
    .map(t => [...t.querySelectorAll('tr')]
      .map(tr => [...tr.querySelectorAll('th,td')].map(c => limpio(c.innerText)))
      .filter(f => f.some(c => c)))
    .filter(t => t.length > 1);
  const enlaces = [...document.querySelectorAll('a[href]')]
    .map(a => ({texto: limpio(a.innerText), url: a.href}))
    .filter(e => e.texto && /^https?:/.test(e.url));
  const raiz = document.querySelector('main, [role=main], .region-content') || document.body;
  const lineas = raiz ? (raiz.innerText || '').split('\n').map(limpio).filter(Boolean) : [];
  return {titulo: document.title, lineas, tablas, enlaces};
}
"""

SELECTOR_SIGUIENTE = "a[rel='next'], li.pager__item--next a, li.next:not(.disabled) a"
MAX_LINEAS = 500
MAX_ENLACES = 300


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def resolver_url(filtros: dict) -> str:
    if filtros.get("url"):
        url = filtros["url"]
        if url.startswith("/"):
            url = config.BASE_URL + url
    elif filtros.get("modulo") in config.MODULOS:
        url = config.BASE_URL + config.MODULOS[filtros["modulo"]]["ruta"]
    else:
        raise ValueError("Indique 'modulo' (ver GET /modulos) o 'url'.")
    if not url_permitida(url):
        raise ValueError(f"URL fuera de los dominios permitidos: {url}")
    return url


def _asentar(nav: Navegador) -> None:
    try:
        nav.page.wait_for_load_state("networkidle", timeout=8000)
    except PWTimeout:
        pass


def extraer_pagina(nav: Navegador, termino: Optional[str]) -> dict:
    partes = []
    for frame in nav.page.frames:  # el primero es el documento principal
        try:
            partes.append(frame.evaluate(JS_EXTRAER))
        except PWError:
            continue
    lineas, tablas, enlaces, vistos = [], [], [], set()
    for p in partes:
        lineas += p["lineas"]
        tablas += p["tablas"]
        for e in p["enlaces"]:
            if e["url"] not in vistos:
                vistos.add(e["url"])
                enlaces.append(e)
    documentos = [e for e in enlaces if urlparse(e["url"]).path.lower().endswith(config.EXTENSIONES_DOCUMENTO)]
    if termino:
        t = _norm(termino)
        lineas = [l for l in lineas if t in _norm(l)]
        # Se conserva la primera fila de cada tabla (encabezado) y las filas que contienen el termino.
        tablas = [[f for i, f in enumerate(tb) if i == 0 or any(t in _norm(c) for c in f)] for tb in tablas]
        tablas = [tb for tb in tablas if len(tb) > 1]
        enlaces = [e for e in enlaces if t in _norm(e["texto"])]
        documentos = [e for e in documentos if t in _norm(e["texto"])]
    return {
        "url": nav.page.url,
        "titulo": partes[0]["titulo"] if partes else "",
        "texto": lineas[:MAX_LINEAS],
        "tablas": tablas,
        "enlaces": enlaces[:MAX_ENLACES],
        "documentos": documentos,
        "truncado": len(lineas) > MAX_LINEAS or len(enlaces) > MAX_ENLACES,
    }


def pagina_siguiente(nav: Navegador) -> bool:
    loc = nav.page.locator(SELECTOR_SIGUIENTE)
    if loc.count() == 0:
        return False
    destino = loc.first.evaluate("a => a.href") or ""
    nav.pausa()
    if destino.startswith("http") and destino.split("#")[0] != nav.page.url.split("#")[0]:
        nav.ir(destino, requiere_sesion=False)
    else:
        loc.first.click()
        nav.page.wait_for_load_state("domcontentloaded")
    return True


def descargar(nav: Navegador, url: str, consulta_id: str) -> dict:
    if not url_permitida(url):
        return {"url": url, "error": "dominio no permitido"}
    try:
        r = nav.ctx.request.get(url, timeout=60000)
    except PWError as e:
        return {"url": url, "error": str(e).splitlines()[0]}
    if not r.ok:
        return {"url": url, "error": f"HTTP {r.status}"}
    carpeta = config.DESCARGAS / consulta_id
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre = re.sub(r"[^\w.\-]+", "_", urlparse(url).path.rsplit("/", 1)[-1]) or "documento"
    ruta = carpeta / nombre
    cuerpo = r.body()
    ruta.write_bytes(cuerpo)
    return {"url": url, "archivo": str(ruta), "bytes": len(cuerpo)}


def ejecutar(nav: Navegador, consulta_id: str, filtros: dict) -> dict:
    url = resolver_url(filtros)
    estado_http = nav.ir(url, requiere_sesion=filtros.get("requiere_sesion", True))
    if estado_http in (401, 403):
        raise AccesoDenegado(
            f"HTTP {estado_http} en {url}: la cuenta en uso no tiene acceso a este contenido "
            "(o el WAF del sitio rechazo la peticion, como ocurre con HEADLESS=true)."
        )

    paginas, visitadas = [], set()
    maximo = int(filtros.get("max_paginas", 1))
    for n in range(maximo):
        _asentar(nav)
        if nav.page.url in visitadas:
            break
        visitadas.add(nav.page.url)
        paginas.append(extraer_pagina(nav, filtros.get("termino")))
        if n + 1 >= maximo or not pagina_siguiente(nav):
            break

    descargas = []
    if filtros.get("descargar_documentos"):
        pendientes, vistos = [], set()
        for p in paginas:
            for d in p["documentos"]:
                if d["url"] not in vistos:
                    vistos.add(d["url"])
                    pendientes.append(d["url"])
        for u in pendientes[: int(filtros.get("max_documentos", 5))]:
            nav.pausa()
            descargas.append(descargar(nav, u, consulta_id))

    return {
        "url_inicial": url,
        "http_status": estado_http,
        "modo_login": nav.modo_login,
        "paginas": paginas,
        "descargas": descargas,
        "resumen": {
            "paginas": len(paginas),
            "lineas_texto": sum(len(p["texto"]) for p in paginas),
            "tablas": sum(len(p["tablas"]) for p in paginas),
            "filas": sum(len(t) for p in paginas for t in p["tablas"]),
            "enlaces": sum(len(p["enlaces"]) for p in paginas),
            "documentos_encontrados": sum(len(p["documentos"]) for p in paginas),
            "documentos_descargados": sum(1 for d in descargas if "archivo" in d),
        },
    }
