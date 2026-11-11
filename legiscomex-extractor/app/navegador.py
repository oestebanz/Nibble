"""Navegador unico (Chrome real via Playwright) con perfil persistente y login.

El perfil vive en data/perfil-chrome, asi que la sesion sobrevive a reinicios del API
y el login (con su reCAPTCHA) solo se hace cuando la sesion realmente expira.
Todo este modulo debe usarse desde un solo hilo (ver app/trabajador.py).
"""
import random
import time
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from . import config


class ErrorLogin(Exception):
    pass


def url_permitida(url: str) -> bool:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host == d or host.endswith("." + d) for d in config.DOMINIOS_PERMITIDOS)


class Navegador:
    def __init__(self) -> None:
        self._pw = None
        self.ctx = None
        self.page = None
        self.ultimo_login: Optional[float] = None
        self.modo_login: Optional[str] = None
        self.sesion_verificada = False

    def iniciar(self) -> None:
        if self.ctx is not None:
            return
        config.PERFIL.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(config.PERFIL),
            channel="chrome",
            headless=config.HEADLESS,
            accept_downloads=True,
            viewport={"width": 1366, "height": 850},
            locale="es-CO",
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def cerrar(self) -> None:
        try:
            if self.ctx is not None:
                self.ctx.close()
            if self._pw is not None:
                self._pw.stop()
        except PWError:
            pass
        finally:
            self.ctx = self.page = self._pw = None
            self.sesion_verificada = False

    def pausa(self) -> None:
        time.sleep(random.uniform(config.PAUSA_MIN_S, config.PAUSA_MAX_S))

    def en_sso(self) -> bool:
        return (urlparse(self.page.url).hostname or "") == config.SSO_HOST

    def _fuera_del_sso(self, timeout_s: float) -> bool:
        try:
            self.page.wait_for_url(
                lambda u: (urlparse(u).hostname or "") != config.SSO_HOST,
                timeout=timeout_s * 1000,
                wait_until="domcontentloaded",
            )
            return True
        except PWTimeout:
            return False

    def _error_del_formulario(self) -> str:
        for sel in (".alert-error", ".kc-feedback-text", "#input-error", ".alert"):
            loc = self.page.locator(sel)
            if loc.count():
                texto = (loc.first.inner_text() or "").strip()
                if texto:
                    return texto
        return ""

    def _abrir_login(self) -> None:
        resp = self.page.goto(config.LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
        if resp is not None and resp.status >= 400:
            raise ErrorLogin(
                f"El sitio respondio HTTP {resp.status} al abrir el login. Su WAF (CloudFront) rechaza Chrome "
                "sin ventana: use HEADLESS=false."
            )

    def _hay_sesion(self) -> bool:
        """Comprueba en la portada que el sitio ya no ofrece el boton 'Iniciar sesion'."""
        resp = self.page.goto(config.BASE_URL, wait_until="domcontentloaded", timeout=45000)
        if resp is None or resp.status >= 400:
            return False
        cabecera = self.page.locator(".contentUser").count() > 0
        return cabecera and self.page.locator("button.inicioSesion").count() == 0

    def login(self) -> str:
        """Deja la sesion iniciada. Devuelve como se logro: sesion_existente | automatico | manual."""
        self.iniciar()
        self._abrir_login()
        if not self.en_sso():
            if self._hay_sesion():
                return self._login_ok("sesion_existente")
            raise ErrorLogin("El sitio no llevo al formulario del SSO y tampoco muestra una sesion iniciada.")

        detalle = "sin credenciales en .env"
        if config.USUARIO and config.CLAVE:
            self.page.locator("#username").fill(config.USUARIO)
            self.page.locator("#password").fill(config.CLAVE)
            try:  # "Seguir conectado": la sesion del SSO dura mas y se repiten menos logins
                self.page.locator("#rememberMe").check(timeout=3000)
            except PWError:
                pass
            # El boton lleva reCAPTCHA atado: al hacer clic, la pagina pide el token y envia el formulario.
            self.page.locator("#kc-login").click()
            if self._fuera_del_sso(30):
                if self._hay_sesion():
                    return self._login_ok("automatico")
                detalle = "el SSO redirigio pero el sitio no muestra la sesion iniciada"
                self._abrir_login()  # de vuelta al formulario para el respaldo manual
            else:
                detalle = self._error_del_formulario() or "el SSO no redirigio tras enviar el formulario"

        if config.HEADLESS:
            raise ErrorLogin(f"Login automatico fallido ({detalle}). Con HEADLESS=true no hay ventana para completarlo a mano.")

        # Respaldo: una persona completa el login en la ventana abierta.
        if self._fuera_del_sso(config.ESPERA_LOGIN_MANUAL_S) and self._hay_sesion():
            return self._login_ok("manual")
        raise ErrorLogin(
            f"Login no completado ({detalle}). Se espero {config.ESPERA_LOGIN_MANUAL_S}s a que se completara en la ventana de Chrome."
        )

    def _login_ok(self, modo: str) -> str:
        self.ultimo_login = time.time()
        self.modo_login = modo
        self.sesion_verificada = True
        return modo

    def asegurar_sesion(self) -> None:
        if not self.sesion_verificada:
            self.login()

    def ir(self, url: str, requiere_sesion: bool = True) -> Optional[int]:
        """Navega a url y devuelve el estado HTTP.

        Si el sitio manda al SSO (sesion vencida), hace login y reintenta una vez.
        """
        if not url_permitida(url):
            raise ValueError(f"URL fuera de los dominios permitidos: {url}")
        self.iniciar()
        if requiere_sesion:
            self.asegurar_sesion()
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if self.en_sso():
            self.sesion_verificada = False
            self.login()
            resp = self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if self.en_sso():
                raise ErrorLogin(f"El sitio sigue pidiendo login para {url} despues de iniciar sesion.")
        return resp.status if resp is not None else None

    def estado(self) -> dict:
        return {
            "navegador_abierto": self.ctx is not None,
            "sesion_verificada": self.sesion_verificada,
            "modo_login": self.modo_login,
            "ultimo_login": self.ultimo_login,
            "credenciales_en_env": bool(config.USUARIO and config.CLAVE),
            "headless": config.HEADLESS,
        }
