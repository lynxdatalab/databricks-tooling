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

DOS PASADAS. La primera mira lo que el repo DECLARA. La segunda mira lo que el código HACE: un
`saveAsTable("otro.esquema.t")` o un `spark.sql("INSERT INTO ...")` dentro del cuerpo de un
pipeline escriben donde digan, y eso no aparece en `tables.lock`.

LO QUE NO CUBRE: un destino construido en tiempo de ejecución no se puede resolver leyendo el
código. Se reporta como AVISO —para que se vea en el PR— pero no falla, porque a veces es legítimo.
Y sin revisión obligatoria de PR, toda compuerta a nivel de código es un aviso y no un muro: lo
único no evadible son los grants de la identidad que ejecuta.

Uso:
    check-schemas
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

from .proyecto import (
    ErrorDeProyecto,
    cargar_proveedor,
    leer_configuracion,
    leer_pipelines,
    resolver_raiz,
)
from .tables_lock import _descubrir

#: Métodos del DataFrameWriter que escriben una tabla con nombre. `@dlt.table` no está aquí porque
#: eso ya lo cubre la comprobación de lo declarado: publica en el esquema del recurso, no en uno
#: arbitrario.
ESCRITURAS_METODO = {"saveAsTable", "insertInto"}

#: SQL que escribe. No están SELECT ni las lecturas: leer otro esquema suele ser legítimo —es para
#: lo que existe un lakehouse— y la restricción de lectura vive en los grants, no aquí.
SQL_ESCRITURA = re.compile(
    r"\b(INSERT\s+INTO|INSERT\s+OVERWRITE(?:\s+TABLE)?|CREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(?:EXTERNAL\s+)?(?:TABLE|VIEW)(?:\s+IF\s+NOT\s+EXISTS)?|MERGE\s+INTO|UPDATE|"
    r"DELETE\s+FROM|DROP\s+TABLE(?:\s+IF\s+EXISTS)?|TRUNCATE\s+TABLE|ALTER\s+TABLE)"
    r"\s+([A-Za-z0-9_.`{}]+)",
    re.IGNORECASE,
)


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


def _esquema_de_destino(destino: str) -> str | None:
    """El esquema al que apunta un destino de escritura, o None si no se puede saber.

    Un nombre sin punto es una tabla del esquema propio del pipeline: no dice nada de otro esquema
    y no se reporta. Con dos partes el esquema es la primera; con tres, la segunda.
    """
    limpio = destino.strip().strip("`").replace("`", "")
    if "{" in limpio or "}" in limpio:
        # Un nombre interpolado: no se puede resolver leyendo el código.
        return None
    partes = [p for p in limpio.split(".") if p]
    if len(partes) == 2:
        return partes[0]
    if len(partes) >= 3:
        return partes[1]
    return None


def _escrituras_de_archivo(ruta: Path) -> tuple[list[tuple[int, str, str]], list[tuple[int, str]]]:
    """([(línea, destino, cómo)], [(línea, cómo)] dinámicas) de un archivo de pipeline."""
    texto = ruta.read_text(encoding="utf-8")
    try:
        arbol = ast.parse(texto)
    except SyntaxError:
        # Un archivo que no compila no es asunto de esta compuerta: ruff y pytest lo dirán mejor.
        return [], []

    concretas: list[tuple[int, str, str]] = []
    dinamicas: list[tuple[int, str]] = []

    for nodo in ast.walk(arbol):
        # df.write.saveAsTable("...") / .insertInto("...")
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute):
            if nodo.func.attr in ESCRITURAS_METODO:
                if nodo.args and isinstance(nodo.args[0], ast.Constant):
                    if isinstance(nodo.args[0].value, str):
                        concretas.append((nodo.lineno, nodo.args[0].value, nodo.func.attr))
                        continue
                dinamicas.append((nodo.lineno, nodo.func.attr))

        # SQL en una cadena literal. Las f-strings llegan como JoinedStr y sus trozos constantes
        # se ven aquí sueltos, así que un destino interpolado no produce una coincidencia
        # resoluble — se reporta como dinámico más abajo.
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
            for verbo, destino in SQL_ESCRITURA.findall(nodo.value):
                concretas.append((nodo.lineno, destino, " ".join(verbo.split()).upper()))

        if isinstance(nodo, ast.JoinedStr):
            literal = "".join(
                v.value
                for v in nodo.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
            for verbo, _destino in SQL_ESCRITURA.findall(literal):
                dinamicas.append((nodo.lineno, " ".join(verbo.split()).upper()))

    return concretas, dinamicas


def _revisar_codigo(raiz: Path, permitidos: set[str]) -> tuple[list[str], list[str]]:
    """(errores, avisos) del escaneo de escrituras en los archivos de los pipelines."""
    errores: list[str] = []
    avisos: list[str] = []

    try:
        pipelines = leer_pipelines(raiz)
    except ErrorDeProyecto:
        # Sin resources/ legibles no hay nada que escanear; el resto del tooling ya se queja.
        return [], []

    vistos: set[Path] = set()
    for pipeline in pipelines:
        for archivo in pipeline.archivos:
            if archivo in vistos or not archivo.is_file():
                continue
            vistos.add(archivo)
            concretas, dinamicas = _escrituras_de_archivo(archivo)
            rel = archivo.relative_to(raiz) if archivo.is_relative_to(raiz) else archivo

            for linea, destino, como in concretas:
                esquema = _esquema_de_destino(destino)
                if esquema is not None and esquema not in permitidos:
                    errores.append(f"{rel}:{linea}  {como} → {destino}   (esquema '{esquema}')")

            for linea, como in dinamicas:
                avisos.append(f"{rel}:{linea}  {como} con destino construido en ejecución")

    return errores, avisos


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

    # Segunda pasada: lo que el código HACE, no solo lo que declara. Un `@dlt.table` publica en el
    # esquema del recurso, pero un `saveAsTable("otro.esquema.t")` o un `spark.sql("INSERT INTO
    # ...")` dentro del cuerpo del pipeline escriben donde digan, y eso no aparece en tables.lock.
    errores_codigo, avisos = _revisar_codigo(raiz, permitidos)

    lista = ", ".join(sorted(permitidos))

    if errores_codigo:
        print(
            f"error: {len(errores_codigo)} escritura(s) en el código apuntan fuera de: {lista}\n",
            file=sys.stderr,
        )
        for e in errores_codigo:
            print(f"    {e}", file=sys.stderr)
        return 1

    if avisos:
        # Aviso y no error: construir el nombre en tiempo de ejecución es a veces legítimo, y no se
        # puede saber leyendo el código. Se reporta para que se vea en el PR, que es lo único que
        # esta compuerta puede ofrecer ahí.
        print(f"\n{len(avisos)} escritura(s) con destino no resoluble:")
        for a in avisos:
            print(f"    {a}")
        print("  No se puede saber a qué esquema van. Revísalas a mano.")

    print(f"\n{len(declaradas)} tabla(s) declarada(s), todas dentro de: {lista}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
