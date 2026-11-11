"""Persistencia de consultas en SQLite (un archivo, sin servidor)."""
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from typing import Optional

from . import config

_lock = threading.Lock()
_con: Optional[sqlite3.Connection] = None


def _db() -> sqlite3.Connection:
    global _con
    if _con is None:
        config.DATOS.mkdir(parents=True, exist_ok=True)
        _con = sqlite3.connect(config.BD, check_same_thread=False)
        _con.row_factory = sqlite3.Row
        _con.execute(
            """CREATE TABLE IF NOT EXISTS consultas (
                id TEXT PRIMARY KEY,
                clave TEXT NOT NULL,
                filtros TEXT NOT NULL,
                estado TEXT NOT NULL,
                creado REAL NOT NULL,
                actualizado REAL NOT NULL,
                resultado TEXT,
                error TEXT
            )"""
        )
        _con.execute("CREATE INDEX IF NOT EXISTS ix_clave ON consultas (clave, estado, creado)")
        _con.commit()
    return _con


def clave_de(filtros: dict) -> str:
    return hashlib.sha256(json.dumps(filtros, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _fila(row: Optional[sqlite3.Row], con_resultado: bool = True) -> Optional[dict]:
    if row is None:
        return None
    d = {
        "id": row["id"],
        "estado": row["estado"],
        "filtros": json.loads(row["filtros"]),
        "creado": row["creado"],
        "actualizado": row["actualizado"],
        "error": row["error"],
    }
    if con_resultado:
        d["resultado"] = json.loads(row["resultado"]) if row["resultado"] else None
    return d


def crear(filtros: dict) -> dict:
    ahora = time.time()
    cid = uuid.uuid4().hex[:12]
    with _lock:
        _db().execute(
            "INSERT INTO consultas (id, clave, filtros, estado, creado, actualizado) VALUES (?,?,?,?,?,?)",
            (cid, clave_de(filtros), json.dumps(filtros, ensure_ascii=False), "pendiente", ahora, ahora),
        )
        _db().commit()
    return obtener(cid)


def actualizar(cid: str, estado: str, resultado: Optional[dict] = None, error: Optional[str] = None) -> None:
    with _lock:
        _db().execute(
            "UPDATE consultas SET estado=?, resultado=?, error=?, actualizado=? WHERE id=?",
            (estado, json.dumps(resultado, ensure_ascii=False) if resultado is not None else None, error, time.time(), cid),
        )
        _db().commit()


def obtener(cid: str) -> Optional[dict]:
    with _lock:
        row = _db().execute("SELECT * FROM consultas WHERE id=?", (cid,)).fetchone()
    return _fila(row)


def en_cache(filtros: dict) -> Optional[dict]:
    """Ultima consulta identica completada dentro del TTL, si existe."""
    with _lock:
        row = _db().execute(
            "SELECT * FROM consultas WHERE clave=? AND estado='completada' AND creado>=? ORDER BY creado DESC LIMIT 1",
            (clave_de(filtros), time.time() - config.CACHE_TTL_S),
        ).fetchone()
    return _fila(row)


def recientes(limite: int = 20) -> list:
    with _lock:
        rows = _db().execute("SELECT * FROM consultas ORDER BY creado DESC LIMIT ?", (limite,)).fetchall()
    return [_fila(r, con_resultado=False) for r in rows]


def cerrar_huerfanas() -> int:
    """Marca como fallidas las consultas que quedaron a medias en un arranque anterior."""
    with _lock:
        cur = _db().execute(
            "UPDATE consultas SET estado='fallida', error='El API se reinicio antes de terminar', actualizado=? "
            "WHERE estado IN ('pendiente', 'en_proceso')",
            (time.time(),),
        )
        _db().commit()
    return cur.rowcount
