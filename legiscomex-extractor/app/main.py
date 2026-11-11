"""API del extractor de Legiscomex.

Arranque:  .venv\\Scripts\\python -m uvicorn app.main:app --port 8000
Docs:      http://127.0.0.1:8000/docs
"""
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from . import config, store
from .trabajador import trabajador

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
FINALES = ("completada", "fallida")


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.cerrar_huerfanas()
    trabajador.iniciar()
    yield
    trabajador.detener()


app = FastAPI(title="Extractor Legiscomex", version="0.1.0", lifespan=lifespan)

# El frontend lo desarrolla otra persona y corre en otro puerto: se permite cualquier origen local.
# No se abre a "*" porque este API maneja una sesion autenticada.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class Filtros(BaseModel):
    modulo: Optional[str] = Field(None, description="Clave de GET /modulos, p. ej. 'acuerdos'")
    url: Optional[str] = Field(None, description="Ruta o URL de legiscomex.com, p. ej. '/ir/49'")
    termino: Optional[str] = Field(None, description="Conserva solo lineas, filas y enlaces que contengan este texto")
    max_paginas: int = Field(1, ge=1, le=500, description="Paginas a recorrer siguiendo el paginador")
    descargar_documentos: bool = False
    max_documentos: int = Field(5, ge=0, le=25)
    requiere_sesion: Optional[bool] = Field(None, description="Por defecto true, salvo modulos publicos")

    @model_validator(mode="after")
    def _validar(self):
        if not self.modulo and not self.url:
            raise ValueError("Indique 'modulo' o 'url'.")
        if self.modulo and not self.url and self.modulo not in config.MODULOS:
            raise ValueError(f"Modulo desconocido. Opciones: {', '.join(config.MODULOS)}")
        if self.requiere_sesion is None:
            publico = bool(self.modulo and not self.url and config.MODULOS[self.modulo].get("publico"))
            self.requiere_sesion = not publico
        self.termino = (self.termino or "").strip() or None
        return self


class Pregunta(BaseModel):
    mensaje: str = Field(..., min_length=1)
    sesion_id: Optional[str] = None


@app.get("/")
def raiz():
    return {"servicio": "Extractor Legiscomex", "docs": "/docs", "sesion": trabajador.estado()}


@app.get("/modulos")
def modulos():
    return config.MODULOS


@app.get("/sesion")
def sesion():
    return trabajador.estado()


@app.post("/sesion/login")
def login():
    """Inicia sesion ahora (automatico con .env; si falla, espera a que se complete en la ventana de Chrome)."""
    try:
        return trabajador.pedir_login().result(timeout=config.ESPERA_LOGIN_MANUAL_S + 120)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")


@app.post("/consultas")
def crear_consulta(
    filtros: Filtros,
    esperar_s: int = Query(0, ge=0, le=300, description="Segundos a esperar el resultado antes de responder"),
    forzar: bool = Query(False, description="Ignora el cache y vuelve a consultar el sitio"),
):
    datos = filtros.model_dump()
    if not forzar:
        previa = store.en_cache(datos)
        if previa is not None:
            return {**previa, "cache": True}
    consulta = store.crear(datos)
    trabajador.encolar_consulta(consulta["id"])
    limite = time.time() + esperar_s
    while consulta["estado"] not in FINALES and time.time() < limite:
        time.sleep(0.5)
        consulta = store.obtener(consulta["id"])
    codigo = 200 if consulta["estado"] in FINALES else 202
    return JSONResponse(status_code=codigo, content={**consulta, "cache": False})


@app.get("/consultas")
def listar_consultas(limite: int = Query(20, ge=1, le=200)):
    return store.recientes(limite)


@app.get("/consultas/{consulta_id}")
def obtener_consulta(consulta_id: str):
    consulta = store.obtener(consulta_id)
    if consulta is None:
        raise HTTPException(status_code=404, detail="Consulta no encontrada")
    return consulta


@app.post("/agente")
async def agente(pregunta: Pregunta):
    """Pregunta en lenguaje natural; el agente ADK decide que consultar y resume lo extraido."""
    try:
        from agentes.legiscomex_agent.agent import preguntar
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Agente no disponible: {type(e).__name__}: {e}")
    try:
        return await preguntar(pregunta.mensaje, pregunta.sesion_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")
