"""Configuracion central. Los valores sensibles salen de .env (ver .env.example)."""
import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

BASE_URL = "https://www.legiscomex.com"
LOGIN_URL = f"{BASE_URL}/legis_sso/new_login"
SSO_HOST = "sso.legis.com.co"
# El API solo navega a estos dominios (evita que sirva de proxy a cualquier sitio).
DOMINIOS_PERMITIDOS = ("legiscomex.com", "legis.com.co", "legis.co")

USUARIO = os.getenv("LEGISCOMEX_USER", "").strip()
CLAVE = os.getenv("LEGISCOMEX_PASSWORD", "")
HEADLESS = os.getenv("HEADLESS", "false").strip().lower() == "true"
ESPERA_LOGIN_MANUAL_S = int(os.getenv("ESPERA_LOGIN_MANUAL_S", "180"))
PAUSA_MIN_S = float(os.getenv("PAUSA_MIN_S", "1.5"))
PAUSA_MAX_S = float(os.getenv("PAUSA_MAX_S", "4"))
CACHE_TTL_S = int(os.getenv("CACHE_TTL_S", "3600"))

DATOS = RAIZ / "data"
PERFIL = DATOS / "perfil-chrome"
ESTADO_SESION = DATOS / "estado-sesion.json"
DESCARGAS = DATOS / "descargas"
BD = DATOS / "consultas.db"

# Accesos "Ingresar" de la pagina de inicio de legiscomex.com (observados el 2026-11-10).
MODULOS = {
    "estadisticas": {"ruta": "/ir/49", "nombre": "Estadisticas de Comercio Exterior"},
    "mercados": {"ruta": "/ir/53", "nombre": "Analisis de Mercados Internacionales"},
    "oportunidades": {"ruta": "/ir/13", "nombre": "Oportunidades comerciales"},
    "ranking": {"ruta": "/ir/14", "nombre": "Ranking de Importaciones y Exportaciones"},
    "acuerdos": {"ruta": "/node/15311", "nombre": "Acuerdos Comerciales"},
    "aduanas": {"ruta": "/node/15226", "nombre": "Regimen de Aduanas Colombiano"},
    "guia": {"ruta": "/node/15246", "nombre": "Guia de Importaciones, Exportaciones y Cambios"},
    "simulador": {"ruta": "/ir/17", "nombre": "Simulador de Formularios y Calculadoras"},
    "estudio_mercado": {"ruta": "/ir/66", "nombre": "Guia para realizar un estudio de mercado"},
    "financiera": {"ruta": "/ir/15", "nombre": "Informacion Financiera de Empresas"},
    "universidades": {"ruta": "/ir/16", "nombre": "Universidades ABC del comercio internacional"},
    "logistico": {"ruta": "/ir/57", "nombre": "Modulo Logistico"},
    "noticias": {"ruta": "/blog", "nombre": "Blog y noticias (publico)", "publico": True},
}

EXTENSIONES_DOCUMENTO = (".pdf", ".xls", ".xlsx", ".csv", ".doc", ".docx", ".zip")
