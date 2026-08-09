"""Un repo de mentira, con historia de git de verdad.

Las dos compuertas comparan HEAD contra otra revisión con `git show`, así que probarlas obliga a
tener commits reales. Un mock de git dejaría sin probar justo la parte que falla en producción: la
resolución de la revisión base.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PIPELINE_EJEMPLO = '''
import dlt

@dlt.table(name="pedidos")
def pedidos():
    return spark.read.table("origen")

@dlt.table(name="clientes")
def clientes():
    return spark.read.table("otro")
'''

RECURSO_EJEMPLO = """
resources:
  pipelines:
    silver_ventas:
      name: demo-silver-ventas
      catalog: ${var.catalog_silver}
      schema: ventas
      serverless: true
      libraries:
        - file:
            path: ../pipelines/silver/ventas.py
  jobs:
    batch:
      name: demo-batch
"""


class RepoFalso:
    """Un repo git temporal que se puede mutar y commitear desde una prueba."""

    def __init__(self, raiz: Path) -> None:
        self.raiz = raiz

    def escribir(self, ruta: str, contenido: str) -> Path:
        destino = self.raiz / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(contenido, encoding="utf-8")
        return destino

    def borrar(self, ruta: str) -> None:
        (self.raiz / ruta).unlink()

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=self.raiz, capture_output=True, text=True, check=True
        )
        return proc.stdout

    def commit(self, mensaje: str) -> str:
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", mensaje)
        return self.git("rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> RepoFalso:
    """Repo con un pipeline de silver, su recurso, un tables.lock al día, y un commit."""
    r = RepoFalso(tmp_path)
    r.git("init", "-q", "-b", "main")

    r.escribir("pipelines/silver/ventas.py", PIPELINE_EJEMPLO)
    r.escribir("resources/pipeline_silver_ventas.yml", RECURSO_EJEMPLO)

    # El lock se GENERA, no se escribe a mano: si se copiara el contenido esperado, la prueba de
    # `--check` estaría comparando el fixture contra sí mismo y pasaría aunque el encabezado del
    # generador cambiara.
    generar_lock(r.raiz)

    r.commit("inicial")
    return r


def generar_lock(raiz: Path) -> None:
    """Corre `gen-tables-lock` sobre un repo, en un proceso aparte."""
    subprocess.run(
        [sys.executable, "-m", "lakehouse_tooling.tables_lock", "--root", str(raiz)],
        check=True,
        capture_output=True,
    )
