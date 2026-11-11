"""Hilo unico que es dueno del navegador y procesa las consultas en orden.

Una sola cola = una sola peticion a Legiscomex a la vez, con pausas entre cargas.
"""
import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Optional

from . import extractor, store
from .navegador import ErrorLogin, Navegador

log = logging.getLogger("trabajador")


class Trabajador:
    def __init__(self) -> None:
        self.cola: "queue.Queue" = queue.Queue()
        self.nav = Navegador()
        self._hilo: Optional[threading.Thread] = None

    def iniciar(self) -> None:
        if self._hilo is None or not self._hilo.is_alive():
            self._hilo = threading.Thread(target=self._run, name="navegador", daemon=True)
            self._hilo.start()

    def detener(self) -> None:
        self.cola.put(None)
        if self._hilo is not None:
            self._hilo.join(timeout=20)

    def encolar_consulta(self, consulta_id: str) -> None:
        self.cola.put(("consulta", consulta_id, None))

    def pedir_login(self) -> Future:
        f: Future = Future()
        self.cola.put(("login", None, f))
        return f

    def estado(self) -> dict:
        return {**self.nav.estado(), "en_cola": self.cola.qsize()}

    def _run(self) -> None:
        while True:
            item = self.cola.get()
            if item is None:
                break
            tipo, consulta_id, futuro = item
            try:
                if tipo == "consulta":
                    self._consulta(consulta_id)
                elif tipo == "login":
                    modo = self.nav.login()
                    futuro.set_result({"modo": modo, **self.nav.estado()})
            except Exception as e:  # el hilo no debe morir por una tarea
                log.exception("Fallo la tarea %s", tipo)
                if futuro is not None and not futuro.done():
                    futuro.set_exception(e)
        self.nav.cerrar()

    def _consulta(self, consulta_id: str) -> None:
        consulta = store.obtener(consulta_id)
        if consulta is None:
            return
        store.actualizar(consulta_id, "en_proceso")
        ultimo: Optional[Exception] = None
        for intento in (1, 2):
            try:
                resultado = extractor.ejecutar(self.nav, consulta_id, consulta["filtros"])
                resultado["intentos"] = intento
                store.actualizar(consulta_id, "completada", resultado=resultado)
                return
            except (ErrorLogin, extractor.AccesoDenegado, ValueError) as e:
                # Reintentar no ayuda (y repetir logins fallidos puede bloquear la cuenta).
                store.actualizar(consulta_id, "fallida", error=f"{type(e).__name__}: {e}")
                return
            except Exception as e:
                ultimo = e
                log.warning("Consulta %s, intento %s fallido: %s", consulta_id, intento, e)
                if intento == 1:
                    self.nav.cerrar()  # navegador limpio para el reintento (p. ej. si cerraron la ventana)
                    time.sleep(5)
        store.actualizar(consulta_id, "fallida", error=f"{type(ultimo).__name__}: {ultimo}")


trabajador = Trabajador()
