"""Agente ADK que traduce una peticion en lenguaje natural a consultas del API extractor.

Chat de prueba:  .venv\\Scripts\\adk web agentes      (con el API corriendo en el puerto 8000)
Tambien se usa desde el API en POST /agente.
"""
import os
import uuid
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import InMemoryRunner
from google.genai import types

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

API = os.getenv("EXTRACTOR_API", "http://127.0.0.1:8000").rstrip("/")
MODELO = os.getenv("LLM_MODEL", "openrouter/google/gemini-2.5-flash")
APP = "legiscomex"
ESPERA_S = 240


def _compacto(consulta: dict) -> dict:
    """Recorta el resultado para no gastar tokens: el detalle completo queda en GET /consultas/{id}."""
    resultado = consulta.get("resultado") or {}
    paginas = [
        {
            "url": p["url"],
            "titulo": p["titulo"],
            "texto": "\n".join(p["texto"])[:3000],
            "tablas": [t[:25] for t in p["tablas"][:3]],
            "enlaces": p["enlaces"][:30],
            "documentos": p["documentos"][:15],
        }
        for p in resultado.get("paginas", [])[:3]
    ]
    return {
        "consulta_id": consulta.get("id"),
        "estado": consulta.get("estado"),
        "error": consulta.get("error"),
        "desde_cache": consulta.get("cache", False),
        "resumen": resultado.get("resumen"),
        "descargas": resultado.get("descargas"),
        "paginas": paginas,
    }


async def listar_modulos() -> dict:
    """Devuelve los modulos de Legiscomex que se pueden consultar (clave, nombre y ruta)."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{API}/modulos")
        r.raise_for_status()
        return r.json()


async def consultar_legiscomex(modulo: str, url: str, termino: str, max_paginas: int, descargar_documentos: bool) -> dict:
    """Extrae informacion de Legiscomex con la sesion autenticada del sistema.

    Args:
        modulo: Clave del modulo (ver listar_modulos). Cadena vacia si se usa url.
        url: Ruta o URL de legiscomex.com (p. ej. un enlace devuelto por una consulta previa). Cadena vacia si se usa modulo.
        termino: Texto para filtrar lineas, filas y enlaces. Cadena vacia para no filtrar.
        max_paginas: Paginas a recorrer siguiendo el paginador (1 si no se pide mas).
        descargar_documentos: True para descargar los documentos (PDF, Excel) enlazados.

    Returns:
        Estado de la consulta, resumen de lo extraido y el contenido recortado de las primeras paginas.
    """
    cuerpo = {
        "modulo": modulo or None,
        "url": url or None,
        "termino": termino or None,
        "max_paginas": max(1, min(int(max_paginas or 1), 500)),
        "descargar_documentos": bool(descargar_documentos),
    }
    async with httpx.AsyncClient(timeout=ESPERA_S + 30) as c:
        r = await c.post(f"{API}/consultas", params={"esperar_s": ESPERA_S}, json=cuerpo)
        if r.status_code == 422:
            return {"estado": "rechazada", "error": r.text[:500]}
        r.raise_for_status()
        return _compacto(r.json())


async def estado_consulta(consulta_id: str) -> dict:
    """Consulta el estado y resultado de una consulta previa que seguia en proceso."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{API}/consultas/{consulta_id}")
        if r.status_code == 404:
            return {"estado": "no_encontrada", "consulta_id": consulta_id}
        r.raise_for_status()
        return _compacto(r.json())


root_agent = Agent(
    name="legiscomex_agent",
    model=LiteLlm(model=MODELO),
    description="Consulta y resume informacion de comercio exterior extraida de Legiscomex.",
    instruction=(
        "Eres un analista de comercio exterior. Respondes en espanol usando UNICAMENTE datos obtenidos con tus "
        "herramientas, que extraen informacion de Legiscomex con una sesion ya autenticada.\n"
        "- Llama SIEMPRE a listar_modulos antes de tu primera consulta y usa solo claves de esa lista "
        "(el blog y las noticias son el modulo 'noticias').\n"
        "- Usa consultar_legiscomex con el modulo o la url. Deja termino vacio salvo que el usuario pida filtrar "
        "por una palabra concreta: el filtro descarta todo lo que no la contenga.\n"
        "- Si una herramienta devuelve 'rechazada', corrige los argumentos y vuelve a llamarla; no le pidas al usuario "
        "datos que puedes obtener con listar_modulos.\n"
        "- Puedes profundizar llamando de nuevo con la url de un enlace devuelto.\n"
        "- Si la consulta queda 'en_proceso' o 'pendiente', usa estado_consulta con su consulta_id.\n"
        "- Si el estado es 'fallida' o no hay datos, dilo tal cual con el error recibido; nunca inventes cifras ni documentos.\n"
        "- Cita la URL de origen de cada dato y el consulta_id para trazabilidad."
    ),
    tools=[listar_modulos, consultar_legiscomex, estado_consulta],
)

_runner: Optional[InMemoryRunner] = None


async def preguntar(mensaje: str, sesion_id: Optional[str] = None) -> dict:
    """Ejecuta un turno del agente y devuelve su respuesta final."""
    global _runner
    if _runner is None:
        _runner = InMemoryRunner(agent=root_agent, app_name=APP)
    sid = sesion_id or uuid.uuid4().hex
    servicio = _runner.session_service
    if await servicio.get_session(app_name=APP, user_id="api", session_id=sid) is None:
        await servicio.create_session(app_name=APP, user_id="api", session_id=sid)
    respuesta, herramientas = "", []
    contenido = types.Content(role="user", parts=[types.Part(text=mensaje)])
    async for evento in _runner.run_async(user_id="api", session_id=sid, new_message=contenido):
        for llamada in evento.get_function_calls() or []:
            herramientas.append({"herramienta": llamada.name, "argumentos": dict(llamada.args or {})})
        if evento.is_final_response() and evento.content and evento.content.parts:
            respuesta = "".join(p.text or "" for p in evento.content.parts)
    return {"sesion_id": sid, "respuesta": respuesta, "herramientas_usadas": herramientas}
