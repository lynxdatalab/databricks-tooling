"""Comprueba que el repo solo declare tablas dentro de los esquemas que le corresponden.

POR QUÉ ESTO EXISTE
Los grants acotan lo que puede escribir una PERSONA, no lo que escribe su CÓDIGO. El pipeline no
corre con la identidad de quien lo escribió: corre con la del service principal, que tiene permisos
sobre todo el catálogo de su ambiente. Es un *confused deputy* — al desarrollador no le hacen falta
permisos, le basta con que el SP los tenga.

De modo que un proyecto puede materializar una tabla dentro del esquema de otro sin encontrarse
ningún obstáculo, y sin que nadie se entere hasta que aparece en el catálogo equivocado.

Esta compuerta lo convierte en un fallo del PR. Compara los nombres que el repo DECLARA —los mismos
que alimentan `tables.lock`— contra los esquemas que tiene autorizados:

    [tool.lakehouse-tooling]
    allowed_schemas  = ["ventas"]                             # lista literal
    schemas_provider = "mipaquete.contract:listar_esquemas"   # o dinámico, para repos multi-área

Sin ninguna de las dos, la comprobación se omite: un repo que no declara sus esquemas no está
diciendo "todos", está diciendo "todavía no lo he pensado", y fallar ahí sería ruido.

TAMBIÉN MIRA LOS RETIROS, y esa es la parte que más importa. Un `retire:` sobre una tabla de otro
esquema no la deja inactiva: hace que el job la **borre**. Es la única operación del sistema que
destruye datos, y sería la más fácil de dirigir al sitio equivocado.

LO QUE NO CUBRE: es una comprobación sobre lo DECLARADO. Un nombre construido en tiempo de
ejecución, o un `spark.sql("INSERT INTO otro.esquema...")` dentro del cuerpo de un pipeline, se le
escapan. Sin revisión obligatoria de PR, toda compuerta a nivel de código es un aviso, no un muro:
lo único no evadible son los grants de la identidad que ejecuta.

Uso:
    check-schemas
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .proyecto import ErrorDeProyecto, cargar_proveedor, leer_configuracion, resolver_raiz
from .tables_lock import _descubrir


def esquemas_permitidos(raiz: Path) -> set[str] | None:
    """Los esquemas que este repo tiene autorizados. None = no lo declara, y no se comprueba."""
    proveedor = cargar_proveedor(raiz, "schemas_provider")
    if proveedor is not None:
        # Un proveedor que truena propaga: un contrato ilegible no puede degradar a "ningún esquema
        # permitido", que reportaría como violación absolutamente todo.
        return {str(e) for e in proveedor()}

    literal = leer_configuracion(raiz).get("allowed_schemas")
    if literal is None:
        return None
    if not isinstance(literal, list) or not all(isinstance(e, str) for e in literal):
        raise ErrorDeProyecto(
            "[tool.lakehouse-tooling] allowed_schemas debe ser una lista de cadenas."
        )
    return set(literal)


def _esquema_de(cualificado: str) -> str | None:
    """El esquema de `catalogo.esquema.tabla`. None si el nombre no tiene esa forma."""
    partes = cualificado.split(".")
    return partes[1] if len(partes) == 3 else None


def _declaradas(raiz: Path) -> list[tuple[str, str]]:
    """[(nombre cualificado, origen)] de todo lo que el repo declara: tablas y retiros."""
    nombres, _ = _descubrir(raiz)
    declaradas = [(n, "materializa") for n in nombres]

    retiradas = cargar_proveedor(raiz, "retired_provider")
    if retiradas is not None:
        declaradas += [(str(n), "RETIRA (borra)") for n in retiradas()]

    return sorted(set(declaradas))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="check-schemas",
        description="Comprueba que el repo solo declare tablas en sus esquemas autorizados.",
    )
    p.add_argument("--root", type=Path, default=None, help="Raíz del repo (default: la del git).")
    args = p.parse_args(argv)

    try:
        raiz = resolver_raiz(args.root)
        permitidos = esquemas_permitidos(raiz)
        if permitidos is None:
            print(
                "Sin esquemas declarados: se omite la comprobación.\n"
                "Para activarla, en pyproject.toml:\n\n"
                "    [tool.lakehouse-tooling]\n"
                '    allowed_schemas = ["<el esquema del proyecto>"]'
            )
            return 0
        declaradas = _declaradas(raiz)
    except ErrorDeProyecto as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    fuera: list[tuple[str, str, str]] = []
    sin_forma: list[str] = []

    for cualificado, origen in declaradas:
        esquema = _esquema_de(cualificado)
        if esquema is None:
            sin_forma.append(cualificado)
        elif esquema not in permitidos:
            fuera.append((cualificado, esquema, origen))

    if sin_forma:
        print(
            f"error: {len(sin_forma)} nombre(s) no tienen la forma catalogo.esquema.tabla, "
            "así que no se puede saber a qué esquema van:",
            file=sys.stderr,
        )
        for n in sin_forma:
            print(f"    {n}", file=sys.stderr)
        return 1

    if fuera:
        esperado = ", ".join(sorted(permitidos))
        print(
            f"error: {len(fuera)} tabla(s) declaradas FUERA de los esquemas de este repo.\n"
            f"Autorizados: {esperado}\n",
            file=sys.stderr,
        )
        for cualificado, esquema, origen in fuera:
            print(f"    {cualificado}\n        esquema '{esquema}' — {origen}", file=sys.stderr)
        print(
            "\nLos permisos del service principal NO impiden esto: el pipeline escribe con su\n"
            "identidad, no con la de quien lo programó. Si el repo necesita ese esquema,\n"
            "agrégalo a allowed_schemas en un commit aparte, para que la decisión se vea.",
            file=sys.stderr,
        )
        return 1

    lista = ", ".join(sorted(permitidos))
    print(f"{len(declaradas)} tabla(s) declarada(s), todas dentro de: {lista}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
