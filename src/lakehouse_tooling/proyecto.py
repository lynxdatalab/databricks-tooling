"""Lo que las dos compuertas necesitan saber del repo consumidor.

Tres cosas, y ninguna asume nada sobre el layout más allá de lo que un Databricks Asset Bundle ya
obliga a tener (`databricks.yml` en la raíz, recursos en `resources/*.yml`):

1. **Dónde está la raíz.** Antes se derivaba de ``__file__``, que funcionaba porque el script vivía
   dentro del repo que analizaba. Instalado como paquete, ``__file__`` apunta a site-packages, así
   que la raíz pasa a ser un dato de entrada.
2. **Qué pipelines declara el bundle**, y a qué catálogo y esquema publica cada uno.
3. **Si el repo trae proveedores propios** que reemplacen el descubrimiento por defecto.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ErrorDeProyecto(Exception):
    """El repo consumidor no se puede analizar. El mensaje va dirigido a quien lea el log del CI."""


# --------------------------------------------------------------------------------------------
# Raíz del repo
# --------------------------------------------------------------------------------------------


def resolver_raiz(explicita: Path | None = None) -> Path:
    """Raíz del repo consumidor.

    Prioridad: lo que diga ``--root`` > la raíz del repo git > el directorio actual. El orden
    importa en el CI, donde el working directory es el del checkout pero un `cd` en un `run:` de
    varias líneas puede dejarlo en otro lado.
    """
    if explicita is not None:
        raiz = explicita.resolve()
        if not raiz.is_dir():
            raise ErrorDeProyecto(f"--root apunta a algo que no es un directorio: {raiz}")
        return raiz

    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip()).resolve()

    return Path.cwd().resolve()


# --------------------------------------------------------------------------------------------
# Recursos del bundle
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Pipeline:
    """Un pipeline declarado en ``resources/*.yml``, visto desde fuera del workspace."""

    clave: str
    catalogo: str
    esquema: str
    #: Rutas de los archivos de código que el pipeline carga, ya resueltas contra la raíz.
    archivos: tuple[Path, ...]

    def cualificar(self, tabla: str) -> str:
        """``{catalog_bronze}.ventas.pedidos`` para la tabla ``pedidos`` de este pipeline.

        El catálogo queda como el **nombre de la variable** del bundle, no como su valor por
        ambiente. Si llevara el nombre resuelto habría un `tables.lock` distinto por ambiente y el
        diff contra la rama base dejaría de significar "qué tablas quita este PR". Como efecto
        secundario útil, en un repo multi-capa el placeholder también dice a qué capa pertenece la
        tabla: `{catalog_bronze}` y `{catalog_silver}` no colisionan.
        """
        return f"{self.catalogo}.{self.esquema}.{tabla}"


def _texto_de_referencia(valor: Any) -> str:
    """``${var.catalog_bronze}`` → ``{catalog_bronze}``; un literal se queda como está.

    Reduce la referencia a la variable, no a su valor: el valor depende del target y aquí no hay
    ninguno resuelto.
    """
    if not isinstance(valor, str):
        return str(valor)
    texto = valor.strip()
    if texto.startswith("${") and texto.endswith("}"):
        interior = texto[2:-1]
        if interior.startswith("var."):
            interior = interior[len("var.") :]
        return "{" + interior + "}"
    return texto


def leer_pipelines(raiz: Path, resources: Path | None = None) -> list[Pipeline]:
    """Los pipelines declarados en el bundle, con su catálogo, esquema y archivos de código."""
    directorio = resources or (raiz / "resources")
    if not directorio.is_dir():
        return []

    pipelines: list[Pipeline] = []
    for archivo in sorted(directorio.glob("*.y*ml")):
        try:
            datos = yaml.safe_load(archivo.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            # Un YAML roto lo reporta `bundle validate` con mucho mejor mensaje que el que
            # podríamos dar aquí. Para esta herramienta solo significa "no puedo leerlo".
            continue

        declarados = (datos.get("resources") or {}).get("pipelines") or {}
        for clave, definicion in declarados.items():
            if not isinstance(definicion, dict):
                continue
            pipelines.append(
                Pipeline(
                    clave=clave,
                    catalogo=_texto_de_referencia(definicion.get("catalog", "{catalog}")),
                    esquema=_texto_de_referencia(definicion.get("schema", "default")),
                    archivos=tuple(_archivos_de(definicion, archivo)),
                )
            )
    return pipelines


def _archivos_de(definicion: dict, resource_yml: Path) -> list[Path]:
    """Rutas de ``libraries[].file.path``, resueltas contra el .yml que las declara.

    Las rutas del bundle son relativas al archivo que las contiene (`../pipelines/x.py` desde
    `resources/`), que es justo como las interpreta el CLI de Databricks.
    """
    rutas = []
    for libreria in definicion.get("libraries") or []:
        if not isinstance(libreria, dict):
            continue
        ruta = (libreria.get("file") or {}).get("path")
        if ruta:
            rutas.append((resource_yml.parent / ruta).resolve())
    return rutas


# --------------------------------------------------------------------------------------------
# Proveedores declarados por el repo consumidor
# --------------------------------------------------------------------------------------------


def leer_configuracion(raiz: Path) -> dict[str, Any]:
    """El bloque ``[tool.lakehouse-tooling]`` del ``pyproject.toml`` del consumidor."""
    pyproject = raiz / "pyproject.toml"
    if not pyproject.is_file():
        return {}
    try:
        datos = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return {}
    return (datos.get("tool") or {}).get("lakehouse-tooling") or {}


def cargar_proveedor(raiz: Path, nombre: str):
    """Resuelve ``"paquete.modulo:funcion"`` declarado en la configuración. None si no hay.

    Existe para los repos que generan sus tablas a partir de un contrato en vez de declararlas una
    por una: ahí el escaneo de código no ve nada y la fuente de verdad es el contrato. En vez de
    hacer que esos repos mantengan su propia copia bifurcada de la herramienta —que es exactamente
    lo que pasó antes—, declaran de dónde sacar el dato.

    Un proveedor que truena **propaga** el error. Un contrato ilegible no puede degradar en
    silencio a "cero tablas": eso le diría al CI que el PR borra todo el catálogo, o peor, que no
    borra nada.
    """
    ruta = leer_configuracion(raiz).get(nombre)
    if not ruta:
        return None

    if ":" not in ruta:
        raise ErrorDeProyecto(
            f"[tool.lakehouse-tooling] {nombre} = '{ruta}' no tiene la forma 'modulo:funcion'."
        )
    modulo_nombre, funcion_nombre = ruta.split(":", 1)

    # El código del consumidor casi siempre vive en src/, y en el CI no está necesariamente
    # instalado: `pip install -e .` es opcional y algunos repos solo corren pytest con pythonpath.
    for candidato in (raiz / "src", raiz):
        if candidato.is_dir() and str(candidato) not in sys.path:
            sys.path.insert(0, str(candidato))

    try:
        modulo = importlib.import_module(modulo_nombre)
    except ImportError as exc:
        raise ErrorDeProyecto(
            f"No se pudo importar '{modulo_nombre}', declarado en "
            f"[tool.lakehouse-tooling] {nombre}.\n"
            f"  {exc}"
        ) from exc

    try:
        return getattr(modulo, funcion_nombre)
    except AttributeError as exc:
        raise ErrorDeProyecto(
            f"'{modulo_nombre}' no define '{funcion_nombre}', declarado en "
            f"[tool.lakehouse-tooling] {nombre}."
        ) from exc
