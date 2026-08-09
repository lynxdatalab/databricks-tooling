#!/usr/bin/env python3
"""Genera ``tables.lock``: la lista de tablas que un repo materializa.

Existe para resolver un problema muy concreto. Quitar una tabla de un pipeline **no le pide permiso
al CLI de Databricks**: el recurso `pipeline` no cambió, así que `bundle deploy` no dice nada. La
tabla simplemente queda inactiva en el catálogo y nadie se entera. Sin un artefacto versionado, no
hay forma de que un PR revele "este cambio deja 3 tablas huérfanas en producción".

``tables.lock`` es ese artefacto: generado, versionado y verificado en el CI. El diff contra la rama
base responde **exactamente** qué tablas quita un PR, sin credenciales, sin Spark y sin consultar el
workspace.

CÓMO ENCUENTRA LAS TABLAS
Recorre los pipelines declarados en ``resources/*.yml``, y de cada uno analiza el AST de los
archivos que carga buscando ``@dlt.table(name="...")``, ``@dlt.view(name="...")`` y
``dlt.create_streaming_table("...")`` con nombre **literal**. El catálogo y el esquema salen del
propio recurso, así que una tabla queda como ``{catalog_silver}.ventas.pedidos``: cualificada, y sin
depender del ambiente.

SU LÍMITE, Y CÓMO SE SALE DE ÉL
El escaneo no ve nombres construidos en tiempo de ejecución. Un repo que genera sus tablas en un
bucle a partir de un contrato —patrón correcto y recomendable a escala— declara de dónde sacarlas:

    [tool.lakehouse-tooling]
    tables_provider = "mipaquete.contract:listar_tablas"

La función devuelve los nombres ya cualificados. Con eso, un repo contract-driven usa esta misma
herramienta en vez de mantener su propia copia bifurcada.

Uso:
    gen-tables-lock
    gen-tables-lock --check      # falla si está desactualizado (para el CI)
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

from lakehouse_tooling.proyecto import (
    ErrorDeProyecto,
    Pipeline,
    cargar_proveedor,
    leer_pipelines,
    resolver_raiz,
)

ENCABEZADO = [
    "# tables.lock — GENERADO. No editar a mano.",
    "# Regenerar con:  gen-tables-lock",
    "#",
    "# Las tablas que este repo materializa en Unity Catalog. El CI compara este archivo contra la",
    "# rama base para detectar qué tablas quita un PR: quitarlas del pipeline NO las borra, las",
    "# deja inactivas (consultables, sin actualizarse) sin que el CLI de Databricks avise.",
    "#",
    "# El catálogo va como {nombre_de_variable}, sin resolver por ambiente: con el nombre real",
    "# habría un archivo distinto por ambiente y el diff dejaría de significar",
    "# 'qué tablas quita este PR'.",
]

#: Decoradores y llamadas que declaran una tabla o vista en un pipeline declarativo.
DECORADORES = {"table", "view", "materialized_view"}
LLAMADAS = {"create_streaming_table", "create_target_table"}


def main() -> int:
    args = _parse_args()

    try:
        raiz = resolver_raiz(args.root)
        nombres, dinamicos = _descubrir(raiz)
    except ErrorDeProyecto as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    salida = args.output or (raiz / "tables.lock")
    contenido = "\n".join([*ENCABEZADO, "", *sorted(nombres)]) + "\n"

    if dinamicos:
        print(
            "AVISO: estos archivos declaran tablas con nombres NO literales:\n"
            + "\n".join(f"  {p}" for p in sorted(dinamicos))
            + "\n\nEl escaneo solo ve nombres literales, así que la lista está incompleta y el CI\n"
            "no podrá detectar tablas huérfanas. Declara de dónde sacarlas:\n"
            "  [tool.lakehouse-tooling]\n"
            '  tables_provider = "mipaquete.contract:listar_tablas"',
            file=sys.stderr,
        )

    if args.check:
        if not salida.is_file():
            print(f"error: {salida.name} no existe. Corre  gen-tables-lock", file=sys.stderr)
            return 1
        actual = salida.read_text(encoding="utf-8")
        if actual != contenido:
            _explicar_diferencia(actual, contenido, salida.name)
            return 1
        print(f"{salida.name} está al día ({len(nombres)} tablas).")
        return 0

    salida.write_text(contenido, encoding="utf-8")
    print(f"{salida.name}: {len(nombres)} tablas.")
    return 0


def _descubrir(raiz: Path) -> tuple[set[str], set[str]]:
    """({nombres cualificados}, {archivos con nombres dinámicos})."""
    proveedor = cargar_proveedor(raiz, "tables_provider")
    if proveedor is not None:
        # El repo declaró su propia fuente de verdad. No hay nada que escanear, y por lo tanto
        # tampoco hay "dinámicos" que reportar: los nombres dinámicos son exactamente el problema
        # que el proveedor resuelve.
        return {str(n) for n in proveedor()}, set()

    nombres: set[str] = set()
    dinamicos: set[str] = set()

    for pipeline in leer_pipelines(raiz):
        for archivo in pipeline.archivos:
            literales, tiene_dinamicos = _tablas_de_archivo(archivo)
            nombres.update(pipeline.cualificar(t) for t in literales)
            if tiene_dinamicos:
                dinamicos.add(_relativo(archivo, raiz))

    return nombres, dinamicos


def _relativo(ruta: Path, raiz: Path) -> str:
    try:
        return str(ruta.relative_to(raiz))
    except ValueError:
        return str(ruta)


def _tablas_de_archivo(archivo: Path) -> tuple[set[str], bool]:
    """({nombres literales}, ¿había alguno dinámico?) declarados en un archivo de pipeline."""
    nombres: set[str] = set()
    dinamicos = False

    try:
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
    except (SyntaxError, OSError) as exc:
        print(f"aviso: {archivo.name} no se pudo analizar: {exc}", file=sys.stderr)
        return nombres, dinamicos

    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call):
            literal, dinamico = _nombre_de_llamada(nodo)
            if literal:
                nombres.add(literal)
            dinamicos = dinamicos or dinamico

    return nombres, dinamicos


def _nombre_de_llamada(nodo: ast.Call) -> tuple[str | None, bool]:
    """(nombre literal, ¿era dinámico?) de una llamada que declara una tabla."""
    func = nodo.func
    if not isinstance(func, ast.Attribute):
        return None, False

    # Solo llamadas sobre `dlt`, para no confundir un `spark.table("x")` con una declaración.
    if not (isinstance(func.value, ast.Name) and func.value.id == "dlt"):
        return None, False

    if func.attr in DECORADORES:
        for kw in nodo.keywords:
            if kw.arg == "name":
                return _literal(kw.value)
        return None, False

    if func.attr in LLAMADAS:
        if nodo.args:
            return _literal(nodo.args[0])
        for kw in nodo.keywords:
            if kw.arg == "name":
                return _literal(kw.value)

    return None, False


def _literal(nodo: ast.AST) -> tuple[str | None, bool]:
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
        return nodo.value, False
    # f-strings, variables, concatenaciones: nombre dinámico.
    return None, True


def _explicar_diferencia(actual: str, esperado: str, nombre: str) -> None:
    """Dice QUÉ falta, no solo que no coincide.

    Un "está desactualizado" a secas obliga a regenerar y mirar el diff. Decir cuáles sobran y
    cuáles faltan permite entender el error sin salir del log del CI.
    """

    def solo_tablas(texto: str) -> set[str]:
        return {
            ln.strip()
            for ln in texto.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        }

    en_archivo, esperadas = solo_tablas(actual), solo_tablas(esperado)

    print(
        f"error: {nombre} está desactualizado.\n"
        "Corre  gen-tables-lock  y súbelo en el MISMO PR.",
        file=sys.stderr,
    )
    for etiqueta, conjunto in (
        (f"faltan en {nombre} (las declara el repo)", esperadas - en_archivo),
        (f"sobran en {nombre} (ya no las declara el repo)", en_archivo - esperadas),
    ):
        if conjunto:
            print(f"\n  {etiqueta}:", file=sys.stderr)
            for tabla in sorted(conjunto):
                print(f"    {tabla}", file=sys.stderr)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--root", type=Path, default=None, help="Raíz del repo (default: la de git)."
    )
    parser.add_argument("--output", type=Path, default=None, help="Default: <root>/tables.lock")
    parser.add_argument(
        "--check",
        action="store_true",
        help="No escribe: falla si el archivo no coincide. Para el CI.",
    )
    return parser.parse_args()


__all__ = ["Pipeline", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
