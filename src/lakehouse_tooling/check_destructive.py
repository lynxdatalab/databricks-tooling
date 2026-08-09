#!/usr/bin/env python3
"""Detecta cambios que destruyen datos, comparando contra otra revisión de git.

Los cambios destructivos de Lakeflow/DLT son cuatro, y **el más frecuente no dispara ninguna
alarma**:

  1. Quitar una tabla del pipeline (``enabled: false`` o borrar la entrada).
     → La tabla queda INACTIVA: consultable, sin actualizarse, y nadie la borra nunca.
     → El CLI NO pregunta nada. El recurso `pipeline` no cambió, así que `bundle deploy` calla.
     → Esta herramienta existe principalmente por este caso.

  2. Cambiar `catalog`, `schema`, `target`, `storage` o `serverless` de un pipeline.
     → Fuerza RECREACIÓN: se borra el pipeline y con él sus materialized views y streaming tables.
     → El CLI sí pide confirmación (desde 0.228.0), pero en CI no hay terminal que responda.

  3. Renombrar la clave del recurso o borrar su .yml.
     → Delete + create. Mismo efecto que el 2.

  4. Marcar una tabla como retirada, en los repos que llevan contrato.
     → Se DESTRUYE al vencer el periodo de gracia. Es deliberado, y por eso se separa del caso 1:
       una tabla retirada a propósito y una que se quedó colgada merecen respuestas distintas.

Compara **declaraciones**, no el estado del workspace: lee los archivos en HEAD y en la revisión
base con `git show`. No necesita credenciales, corre en segundos y es determinista — dos personas
que miren el mismo commit ven exactamente lo mismo.

Modos:
  --mode pr      Para el CI de un PR. Falla si hay hallazgos, salvo que el PR lleve la etiqueta
                 correspondiente. Es la compuerta.
  --mode report  Para el job `analizar` del deploy. Nunca falla; publica el resumen y expone
                 `destructive`/`orphaned` como outputs para que los jobs de deploy se etiqueten.

Uso:
    check-destructive --base origin/main --mode pr --labels "acepta-destruccion"
    check-destructive --base HEAD~1 --mode report --output-file "$GITHUB_OUTPUT"
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from lakehouse_tooling.proyecto import ErrorDeProyecto, cargar_proveedor, resolver_raiz

#: Campos de un pipeline que, al cambiar, obligan a Databricks a RECREARLO — lo que borra sus
#: materialized views y streaming tables. No es una lista arbitraria: son los que definen dónde y
#: cómo publica el pipeline, y no se pueden alterar en sitio.
CAMPOS_QUE_RECREAN = ("catalog", "schema", "target", "storage", "serverless")

#: Etiqueta que autoriza cada familia de hallazgos. Ponerlas deja constancia de que fue una
#: decisión.
ETIQUETA_HUERFANAS = "acepta-tablas-inactivas"
ETIQUETA_DESTRUCCION = "acepta-destruccion"


@dataclass
class Hallazgo:
    severidad: str  # DESTRUCTIVO | HUERFANA | AVISO
    titulo: str
    detalle: str
    remedio: str = ""


@dataclass
class Reporte:
    hallazgos: list[Hallazgo] = field(default_factory=list)

    def add(self, *args, **kwargs) -> None:
        self.hallazgos.append(Hallazgo(*args, **kwargs))

    def por_severidad(self, severidad: str) -> list[Hallazgo]:
        return [h for h in self.hallazgos if h.severidad == severidad]

    @property
    def destructivo(self) -> bool:
        return bool(self.por_severidad("DESTRUCTIVO"))

    @property
    def huerfanas(self) -> bool:
        return bool(self.por_severidad("HUERFANA"))


# --------------------------------------------------------------------------------------------
# Lectura de la revisión base
# --------------------------------------------------------------------------------------------


def _git(raiz: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, cwd=raiz, check=False)
    return proc.returncode, proc.stdout


def _leer_en_base(raiz: Path, base: str, ruta: str) -> str | None:
    """Contenido de un archivo en la revisión base. None si allá no existía."""
    codigo, salida = _git(raiz, "show", f"{base}:{ruta}")
    return salida if codigo == 0 else None


def _listar_en_base(raiz: Path, base: str, directorio: str) -> list[str]:
    codigo, salida = _git(raiz, "ls-tree", "--name-only", f"{base}:{directorio}")
    if codigo != 0:
        return []
    return [ln.strip() for ln in salida.splitlines() if ln.strip()]


def _verificar_base(raiz: Path, base: str) -> str | None:
    """Devuelve un mensaje de error si la revisión base no es alcanzable.

    Falla explícito en vez de tratar "no pude ver el antes" como "no cambió nada": ese silencio
    sería exactamente el fallo que esta herramienta existe para evitar. La causa habitual es un
    checkout con `fetch-depth: 1`.
    """
    if _git(raiz, "rev-parse", "--verify", f"{base}^{{commit}}")[0] != 0:
        return (
            f"No se pudo resolver la revisión base '{base}'.\n"
            "Causa habitual: el checkout no trajo historia. "
            "Usa actions/checkout con fetch-depth: 0."
        )
    return None


# --------------------------------------------------------------------------------------------
# A. Recursos del bundle
# --------------------------------------------------------------------------------------------


def _recursos_de_yaml(texto: str, tipo: str) -> dict[str, dict]:
    """{clave del recurso: definición} de los recursos de un tipo en un resources/*.yml."""
    try:
        datos = yaml.safe_load(texto) or {}
    except yaml.YAMLError:
        # Un YAML roto lo reporta el `bundle validate`; aquí solo significa "no puedo comparar".
        return {}
    return (datos.get("resources") or {}).get(tipo) or {}


def _contenidos_de_resources(raiz: Path, base: str | None) -> list[str]:
    """El texto de cada resources/*.yml, en HEAD (base=None) o en la revisión base."""
    if base is None:
        return [
            p.read_text(encoding="utf-8") for p in sorted((raiz / "resources").glob("*.y*ml"))
        ]
    return [
        contenido
        for nombre in _listar_en_base(raiz, base, "resources")
        if nombre.endswith((".yml", ".yaml"))
        if (contenido := _leer_en_base(raiz, base, f"resources/{nombre}")) is not None
    ]


def _recolectar(raiz: Path, base: str | None, tipo: str) -> dict[str, dict]:
    encontrados: dict[str, dict] = {}
    for contenido in _contenidos_de_resources(raiz, base):
        encontrados.update(_recursos_de_yaml(contenido, tipo))
    return encontrados


def revisar_pipelines(raiz: Path, base: str, reporte: Reporte, docs: str) -> None:
    antes = _recolectar(raiz, base, "pipelines")
    ahora = _recolectar(raiz, None, "pipelines")

    for clave in sorted(set(antes) - set(ahora)):
        reporte.add(
            "DESTRUCTIVO",
            f"El pipeline `{clave}` desaparece",
            "Al borrarse un pipeline se borran TODAS sus materialized views y streaming tables.",
            remedio=(
                "Si solo lo estás renombrando, no lo borres: renombra la clave y amarra la nueva "
                "al pipeline existente con "
                f"`databricks bundle deployment bind <clave> <pipeline-id>`. Ver {docs}."
            ),
        )

    for clave in sorted(set(antes) & set(ahora)):
        for campo in CAMPOS_QUE_RECREAN:
            viejo, nuevo = antes[clave].get(campo), ahora[clave].get(campo)
            if viejo != nuevo:
                reporte.add(
                    "DESTRUCTIVO",
                    f"El pipeline `{clave}` cambia `{campo}`",
                    f"`{viejo}` → `{nuevo}`. Cambiar este campo obliga a RECREAR el pipeline, "
                    "y al recrearlo se borran sus materialized views y streaming tables.",
                    remedio=(
                        "Si el objetivo es mover tablas y no perderlas, considera "
                        "`ALTER STREAMING TABLE ... "
                        'SET TBLPROPERTIES("pipelines.pipelineId"=...)`. '
                        f"Ver {docs}."
                    ),
                )

    # Los jobs no guardan datos: borrarlos es recuperable y no merece detener un PR.
    antes_jobs = _recolectar(raiz, base, "jobs")
    ahora_jobs = _recolectar(raiz, None, "jobs")
    for clave in sorted(set(antes_jobs) - set(ahora_jobs)):
        reporte.add(
            "AVISO",
            f"El job `{clave}` desaparece",
            "No borra datos, pero si estaba programado, deja de correr.",
        )


# --------------------------------------------------------------------------------------------
# B. Tablas declaradas (tables.lock)
# --------------------------------------------------------------------------------------------


def _tablas_de_lock(texto: str | None) -> set[str]:
    if texto is None:
        return set()
    return {
        ln.strip() for ln in texto.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    }


def _nombres_retirados(raiz: Path) -> dict[str, str]:
    """{nombre cualificado: motivo} de las tablas que el repo marcó como retiradas.

    Solo aplica a repos que llevan contrato y declaran `retired_provider`. Sin él, no hay forma de
    distinguir una tabla retirada a propósito de una que se cayó del pipeline por descuido, y
    ambas se reportan como huérfanas — que es el default prudente.
    """
    proveedor = cargar_proveedor(raiz, "retired_provider")
    if proveedor is None:
        return {}
    return {str(k): str(v) for k, v in dict(proveedor()).items()}


def revisar_tablas(raiz: Path, base: str, reporte: Reporte, lock: Path, docs: str) -> None:
    if not lock.is_file():
        reporte.add(
            "AVISO",
            "No hay `tables.lock`",
            "Sin él no se puede detectar qué tablas quita un PR. Genéralo con `gen-tables-lock`.",
        )
        return

    antes = _tablas_de_lock(_leer_en_base(raiz, base, lock.name))
    ahora = _tablas_de_lock(lock.read_text(encoding="utf-8"))

    quitadas = sorted(antes - ahora)
    if not quitadas:
        return

    # Distinguir "retirada explícitamente" de "solo apagada" es la diferencia entre una destrucción
    # deliberada y una tabla que se queda colgada para siempre. Se reportan por separado.
    retiradas = _nombres_retirados(raiz)
    con_retiro = [t for t in quitadas if t in retiradas]
    sin_retiro = [t for t in quitadas if t not in retiradas]

    if sin_retiro:
        reporte.add(
            "HUERFANA",
            f"{len(sin_retiro)} tabla(s) dejan de construirse",
            "\n".join(f"  {t} → quedará INACTIVA" for t in sin_retiro)
            + "\n\nSeguirán consultables pero NO se actualizarán, y **no se borran solas**.",
            remedio=(
                f"Si quieres conservarlas así, etiqueta el PR con `{ETIQUETA_HUERFANAS}`.\n"
                "Si quieres DESTRUIRLAS, hace falta un `DROP TABLE` explícito ejecutado por la "
                "identidad dueña de las tablas — las posee el service principal del pipeline, así "
                f"que una persona no puede borrarlas. Ver {docs}."
            ),
        )

    if con_retiro:
        reporte.add(
            "DESTRUCTIVO",
            f"{len(con_retiro)} tabla(s) marcadas para DESTRUIRSE",
            "\n".join(f"  {t} → {retiradas[t]}" for t in con_retiro)
            + "\n\nSe borrarán al vencer su periodo de gracia. `UNDROP` da 7 días después.",
            remedio=f"Si es correcto, etiqueta el PR con `{ETIQUETA_DESTRUCCION}`.",
        )


# --------------------------------------------------------------------------------------------
# Salida
# --------------------------------------------------------------------------------------------


def render(reporte: Reporte, etiquetas: set[str], modo: str, docs: str) -> str:
    if not reporte.hallazgos:
        return (
            "### Análisis de cambios ✅\n\n"
            "Este cambio no borra recursos ni deja tablas inactivas.\n"
        )

    lineas = ["### Análisis de cambios", ""]

    for severidad, titulo, icono in (
        ("DESTRUCTIVO", "Se van a DESTRUIR datos", "🔴"),
        ("HUERFANA", "Tablas que quedarán inactivas", "🟡"),
        ("AVISO", "Avisos", "ℹ️"),
    ):
        grupo = reporte.por_severidad(severidad)
        if not grupo:
            continue
        lineas += [f"#### {icono} {titulo}", ""]
        for h in grupo:
            lineas += [f"**{h.titulo}**", "", "```", h.detalle, "```", ""]
            if h.remedio:
                lineas += [h.remedio, ""]

    if modo == "pr":
        lineas += ["#### Cómo desbloquear", ""]
        for etiqueta, aplica in (
            (ETIQUETA_DESTRUCCION, reporte.destructivo),
            (ETIQUETA_HUERFANAS, reporte.huerfanas),
        ):
            if aplica:
                estado = "✅ puesta" if etiqueta in etiquetas else "❌ falta"
                lineas.append(f"- Etiqueta `{etiqueta}` — {estado}")
        lineas += [
            "",
            "Poner la etiqueta deja constancia de que fue una decisión y no un descuido.",
            "",
        ]
    else:
        lineas += [
            "#### Antes de aprobar",
            "",
            "Si vas a aprobar un despliegue **destructivo**, confirma que lo de arriba es lo que",
            "esperabas. El despliegue solo lo ejecuta si alguien marcó `allow-destructive` al",
            "lanzar el workflow; si no, se detiene solo.",
            "",
            f"Alternativas que evitan destruir: {docs}.",
            "",
        ]

    return "\n".join(lineas)


def main() -> int:
    args = _parse_args()

    try:
        raiz = resolver_raiz(args.root)
    except ErrorDeProyecto as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if (error := _verificar_base(raiz, args.base)) is not None:
        print(f"error: {error}", file=sys.stderr)
        return 2

    reporte = Reporte()
    try:
        revisar_pipelines(raiz, args.base, reporte, args.docs)
        revisar_tablas(raiz, args.base, reporte, args.lock or (raiz / "tables.lock"), args.docs)
    except ErrorDeProyecto as exc:
        # Un proveedor roto no puede degradar a "no encontré nada": eso convertiría un error de
        # configuración en un visto bueno.
        print(f"error: {exc}", file=sys.stderr)
        return 2

    etiquetas = {e.strip() for e in (args.labels or "").split(",") if e.strip()}
    resumen = render(reporte, etiquetas, args.mode, args.docs)

    print(resumen)
    if args.summary_file:
        with open(args.summary_file, "a", encoding="utf-8") as fh:
            fh.write(resumen + "\n")

    if args.output_file:
        with open(args.output_file, "a", encoding="utf-8") as fh:
            fh.write(f"destructive={str(reporte.destructivo).lower()}\n")
            fh.write(f"orphaned={str(reporte.huerfanas).lower()}\n")

    if args.mode == "report":
        # Nunca falla: es el job que informa a quien va a aprobar. Teñir de rojo la corrida aquí
        # impediría llegar a los jobs de deploy, que es justo lo que se quiere permitir.
        return 0

    faltantes = []
    if reporte.destructivo and ETIQUETA_DESTRUCCION not in etiquetas:
        faltantes.append(ETIQUETA_DESTRUCCION)
    if reporte.huerfanas and ETIQUETA_HUERFANAS not in etiquetas:
        faltantes.append(ETIQUETA_HUERFANAS)

    if faltantes:
        print(f"\nerror: faltan etiquetas en el PR: {', '.join(faltantes)}", file=sys.stderr)
        return 1

    if reporte.hallazgos:
        print("\nHallazgos aceptados por etiqueta.")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base", default="origin/main", help="Revisión contra la que comparar.")
    parser.add_argument("--mode", choices=["pr", "report"], default="pr")
    parser.add_argument("--labels", default="", help="Etiquetas del PR, separadas por coma.")
    parser.add_argument(
        "--root", type=Path, default=None, help="Raíz del repo (default: la de git)."
    )
    parser.add_argument("--lock", type=Path, default=None, help="Default: <root>/tables.lock")
    parser.add_argument(
        "--docs",
        default="la documentación de acciones destructivas del repo",
        help="Dónde están documentadas las alternativas. Se cita en los remedios.",
    )
    parser.add_argument("--summary-file", default=None, help="Ruta de GITHUB_STEP_SUMMARY.")
    parser.add_argument("--output-file", default=None, help="Ruta de GITHUB_OUTPUT.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
