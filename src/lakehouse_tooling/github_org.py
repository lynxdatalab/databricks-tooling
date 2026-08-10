"""Mantiene consistentes los nombres de organización de GitHub que el repo tiene escritos a mano.

POR QUÉ ESTO EXISTE
GitHub no deja parametrizar la organización donde de verdad importa. El `uses:` de un workflow
reutilizable no admite expresiones (``uses: ${{ vars.ORG }}/repo/...@v1`` es un error de sintaxis,
no una variable sin resolver), pip no expande variables dentro de una dependencia
``git+https://github.com/...``, y CODEOWNERS no resuelve nada: son tres literales que hay que
escribir a mano en tres formatos distintos.

DOS ORGANIZACIONES, NO UNA
La primera versión asumía que todas las referencias apuntaban al mismo sitio. Deja de ser cierto en
cuanto el tooling compartido vive en la organización del proveedor y los repos de proyecto en la
del cliente — que es el caso normal, no la excepción. Son dos cosas distintas:

* ``github_org``  — quién es DUEÑO de este repo. Rige los equipos de CODEOWNERS.
* ``tooling_org`` — dónde vive el TOOLING compartido. Rige el ``uses:`` de los workflows
  reutilizables y la dependencia ``git+https``.

Migrar un repo de organización mueve lo primero y **no debe tocar lo segundo**. Confundirlos deja
el CI con ``workflow was not found``, un error que no menciona la organización por ningún lado.

``tooling_org`` ausente equivale a ``github_org``: es lo que mantiene válidos los repos que se
escribieron cuando solo había una organización.

    [tool.lakehouse-tooling]
    github_org  = "org-del-cliente"
    tooling_org = "org-del-proveedor"   # opcional
    tooling_repo = "databricks-tooling" # opcional

Uso:
    set-github-org --check
    set-github-org --org nueva-org
    set-github-org --org org-cliente --tooling-org org-proveedor
    set-github-org --org nueva --from vieja     # primera adopción, sin declaración previa
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .proyecto import ErrorDeProyecto, resolver_raiz

# Una organización o usuario de GitHub: alfanumérico y guiones medios, sin empezar ni terminar en
# guion. Se captura en el grupo `org`.
_ORG = r"(?P<org>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)"
_REPO = r"(?P<repo>[\w.-]+)"

ROL_REPO = "repo"
ROL_TOOLING = "tooling"

TOOLING_REPO_DEFAULT = "databricks-tooling"

# (clase legible, rol, patrón). El rol decide contra qué organización se compara cada referencia.
#
# Los dos patrones de rol `tooling` capturan también el REPO referenciado, y quien los usa descarta
# los que no son el repo de tooling. Sin eso, `uses:` casaría con cualquier
# `org/loquesea/.github/workflows/` y `git+https` con cualquier dependencia alojada en GitHub: hoy
# funcionaría porque solo hay un tooling, y dejaría de funcionar en silencio el día que el repo
# dependa de otra cosa.
#
# El patrón de `uses:` exige además `/.github/workflows/` para NO tocar las acciones de terceros:
# `actions/checkout@v4` y `databricks/setup-cli@v1.11.0` también tienen forma `org/repo`, y
# renombrarlas rompería el CI en vez de migrarlo.
PATRONES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "uses: de un workflow reutilizable",
        ROL_TOOLING,
        re.compile(rf"(?m)^\s*uses:\s*{_ORG}/{_REPO}/\.github/workflows/"),
    ),
    (
        "dependencia git+https",
        ROL_TOOLING,
        re.compile(rf"git\+https://github\.com/{_ORG}/{_REPO}"),
    ),
    (
        "equipo en CODEOWNERS",
        ROL_REPO,
        re.compile(rf"@{_ORG}/[\w.-]+"),
    ),
    (
        "declaración github_org",
        ROL_REPO,
        re.compile(rf'(?m)^\s*github_org\s*=\s*"{_ORG}"'),
    ),
    (
        "declaración tooling_org",
        ROL_TOOLING,
        re.compile(rf'(?m)^\s*tooling_org\s*=\s*"{_ORG}"'),
    ),
)


def _archivos(raiz: Path) -> list[Path]:
    """Dónde puede haber referencias. `docs/` queda fuera a propósito — ver el README."""
    rutas: list[Path] = []
    for patron in ("pyproject.toml", "CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"):
        ruta = raiz / patron
        if ruta.is_file():
            rutas.append(ruta)
    directorio = raiz / ".github" / "workflows"
    if directorio.is_dir():
        rutas.extend(sorted(p for p in directorio.iterdir() if p.suffix in (".yml", ".yaml")))
    return rutas


@dataclass(frozen=True)
class Config:
    """Lo declarado en ``[tool.lakehouse-tooling]``."""

    github_org: str | None
    tooling_org: str | None
    tooling_repo: str

    def esperada(self, rol: str) -> str | None:
        """Qué organización debe tener una referencia de este rol."""
        if rol == ROL_TOOLING:
            # El default importa: mantiene válidos los repos escritos cuando solo había una
            # organización, y evita que mover el tag `v1` los ponga en rojo.
            return self.tooling_org or self.github_org
        return self.github_org


@dataclass(frozen=True)
class Referencia:
    """Una mención de una organización, ubicada para que el mensaje del CI sea accionable."""

    archivo: Path
    linea: int
    org: str
    rol: str
    clase: str
    texto: str

    def ubicacion(self, raiz: Path) -> str:
        return f"{self.archivo.relative_to(raiz)}:{self.linea}"


def _declaracion(texto: str, clave: str) -> str | None:
    m = re.search(rf'(?m)^\s*{clave}\s*=\s*"([^"]*)"', texto)
    return m.group(1) if m else None


def leer_config(raiz: Path) -> Config:
    """Lee la declaración de pyproject.toml.

    Con regex y no con tomllib a propósito: el resto del módulo trabaja sobre el texto crudo para
    preservar comentarios y formato al reescribir, y mezclar las dos lecturas invitaría a que se
    desincronicen.
    """
    ruta = raiz / "pyproject.toml"
    if not ruta.is_file():
        return Config(None, None, TOOLING_REPO_DEFAULT)
    texto = ruta.read_text(encoding="utf-8")
    return Config(
        github_org=_declaracion(texto, "github_org"),
        tooling_org=_declaracion(texto, "tooling_org"),
        tooling_repo=_declaracion(texto, "tooling_repo") or TOOLING_REPO_DEFAULT,
    )


# Compatibilidad: había código y pruebas llamando a esto.
def org_declarada(raiz: Path) -> str | None:
    return leer_config(raiz).github_org


def buscar(raiz: Path, cfg: Config | None = None) -> list[Referencia]:
    """Las referencias que este comando gobierna, en orden de archivo y línea.

    Las referencias de rol `tooling` que apuntan a un repo distinto de ``tooling_repo`` se
    descartan: son dependencias de terceros y no le incumben a este comando.
    """
    cfg = cfg or leer_config(raiz)
    encontradas: list[Referencia] = []
    for ruta in _archivos(raiz):
        texto = ruta.read_text(encoding="utf-8")
        for clase, rol, patron in PATRONES:
            for m in patron.finditer(texto):
                if rol == ROL_TOOLING and "repo" in m.groupdict():
                    if m.group("repo") != cfg.tooling_repo:
                        continue
                encontradas.append(
                    Referencia(
                        archivo=ruta,
                        linea=texto.count("\n", 0, m.start()) + 1,
                        org=m.group("org"),
                        rol=rol,
                        clase=clase,
                        texto=m.group(0).strip(),
                    )
                )
    return sorted(encontradas, key=lambda r: (str(r.archivo), r.linea))


def reescribir(raiz: Path, cfg: Config, cambios: dict[str, tuple[str, str]]) -> list[Referencia]:
    """Aplica {rol: (vieja, nueva)} sobre las referencias de ese rol. Devuelve las tocadas."""
    tocadas: list[Referencia] = []
    for ruta in _archivos(raiz):
        original = ruta.read_text(encoding="utf-8")
        texto = original
        for clase, rol, patron in PATRONES:
            if rol not in cambios:
                continue
            vieja, nueva = cambios[rol]
            if vieja == nueva:
                continue

            def _aplica(m: re.Match[str], rol: str = rol, vieja: str = vieja) -> bool:
                if m.group("org") != vieja:
                    return False
                if rol == ROL_TOOLING and "repo" in m.groupdict():
                    return m.group("repo") == cfg.tooling_repo
                return True

            # Las líneas se calculan sobre el texto ORIGINAL: reemplazar un nombre por otro de
            # distinto largo mueve las columnas, nunca las líneas.
            for m in patron.finditer(original):
                if _aplica(m):
                    tocadas.append(
                        Referencia(
                            archivo=ruta,
                            linea=original.count("\n", 0, m.start()) + 1,
                            org=nueva,
                            rol=rol,
                            clase=clase,
                            texto=m.group(0).strip(),
                        )
                    )

            def _sub(m: re.Match[str], nueva: str = nueva) -> str:
                if not _aplica(m):
                    return m.group(0)
                inicio, fin = m.span("org")
                base = m.start()
                return m.group(0)[: inicio - base] + nueva + m.group(0)[fin - base :]

            texto = patron.sub(_sub, texto)
        if texto != original:
            ruta.write_text(texto, encoding="utf-8")
    return sorted(tocadas, key=lambda r: (str(r.archivo), r.linea))


def _escribir_declaracion(raiz: Path, clave: str, valor: str) -> None:
    """Pone o actualiza `clave = "valor"` dentro de [tool.lakehouse-tooling]."""
    ruta = raiz / "pyproject.toml"
    if not ruta.is_file():
        print(
            f"aviso: no hay pyproject.toml en {raiz}, así que '{clave}' no queda declarada y "
            "`--check` no podrá comprobarla.",
            file=sys.stderr,
        )
        return
    texto = ruta.read_text(encoding="utf-8")
    patron = re.compile(rf'(?m)^(\s*{clave}\s*=\s*")[^"]*(")')
    if patron.search(texto):
        texto = patron.sub(rf"\g<1>{valor}\g<2>", texto, count=1)
    elif "[tool.lakehouse-tooling]" in texto:
        texto = texto.replace(
            "[tool.lakehouse-tooling]", f'[tool.lakehouse-tooling]\n{clave} = "{valor}"', 1
        )
    else:
        texto = texto.rstrip("\n") + f'\n\n[tool.lakehouse-tooling]\n{clave} = "{valor}"\n'
    ruta.write_text(texto, encoding="utf-8")
    print(f"pyproject.toml: {clave} = \"{valor}\"")


def _comprobar(raiz: Path) -> int:
    cfg = leer_config(raiz)
    if cfg.github_org is None:
        print(
            "error: no hay organización declarada. Agrega a pyproject.toml:\n\n"
            "    [tool.lakehouse-tooling]\n"
            '    github_org = "<la organización dueña de este repo>"\n\n'
            "Sin ella nada puede comprobar que los equipos de CODEOWNERS, el `uses:` de los "
            "workflows y la dependencia del tooling apunten a donde deben.",
            file=sys.stderr,
        )
        return 1

    referencias = buscar(raiz, cfg)
    desviadas = [r for r in referencias if r.org != cfg.esperada(r.rol)]

    for r in referencias:
        marca = "  " if r.org == cfg.esperada(r.rol) else "->"
        print(f"{marca} {r.ubicacion(raiz):<44} {r.org:<20} [{r.rol}] {r.clase}")

    por_rol = {
        ROL_REPO: sum(1 for r in referencias if r.rol == ROL_REPO),
        ROL_TOOLING: sum(1 for r in referencias if r.rol == ROL_TOOLING),
    }

    if desviadas:
        print(
            f"\n{len(desviadas)} referencia(s) no apuntan a donde deben.\n"
            f"  dueño del repo : {cfg.github_org}\n"
            f"  tooling        : {cfg.esperada(ROL_TOOLING)}"
            f"{'  (heredado de github_org)' if not cfg.tooling_org else ''}\n\n"
            f"Corrígelas con:  set-github-org --org {cfg.github_org} "
            f"--tooling-org {cfg.esperada(ROL_TOOLING)}",
            file=sys.stderr,
        )
        return 1

    print(
        f"\n{len(referencias)} referencia(s) correctas: "
        f"{por_rol[ROL_REPO]} a '{cfg.github_org}' (dueño del repo), "
        f"{por_rol[ROL_TOOLING]} a '{cfg.esperada(ROL_TOOLING)}' (tooling)."
    )
    return 0


def _aplicar(
    raiz: Path,
    nueva_org: str | None,
    nueva_tooling: str | None,
    desde: str | None,
) -> int:
    cfg = leer_config(raiz)

    vieja_org = desde or cfg.github_org
    if vieja_org is None:
        print(
            "error: no hay organización declarada de la cual partir. Usa --from para decir qué "
            "nombre se está reemplazando la primera vez.",
            file=sys.stderr,
        )
        return 1
    vieja_tooling = cfg.tooling_org or vieja_org

    cambios: dict[str, tuple[str, str]] = {}
    if nueva_org:
        cambios[ROL_REPO] = (vieja_org, nueva_org)

    if nueva_tooling:
        cambios[ROL_TOOLING] = (vieja_tooling, nueva_tooling)
    elif nueva_org and cfg.tooling_org is None:
        # Sin `tooling_org` declarada, el tooling SIGUE a github_org — así se comportaban los repos
        # escritos cuando solo había una organización. Mover solo las referencias del dueño dejaría
        # las del tooling apuntando a la organización vieja mientras `--check` las espera en la
        # nueva: un estado inválido producido por el propio comando.
        #
        # Para separarlos hay que decirlo: `--org NUEVA --tooling-org LA-DE-ANTES`.
        cambios[ROL_TOOLING] = (vieja_tooling, nueva_org)

    tocadas = reescribir(raiz, cfg, cambios)

    if nueva_org:
        _escribir_declaracion(raiz, "github_org", nueva_org)
    if nueva_tooling:
        # Se declara SIEMPRE que se pase, aunque coincida con github_org: dejarla implícita
        # funcionaría hoy y rompería el día que el repo cambie de dueño.
        _escribir_declaracion(raiz, "tooling_org", nueva_tooling)

    for r in tocadas:
        print(f"  {r.ubicacion(raiz):<44} [{r.rol}] {r.clase}")
    print(f"\n{len(tocadas)} referencia(s) movidas.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="set-github-org",
        description="Declara y propaga las organizaciones de GitHub que el repo tiene escritas.",
    )
    p.add_argument("--root", type=Path, default=None, help="Raíz del repo (default: la del git).")
    p.add_argument("--check", action="store_true", help="Solo verifica. Falla si algo no coincide.")
    p.add_argument("--org", help="Organización DUEÑA del repo (equipos de CODEOWNERS).")
    p.add_argument(
        "--tooling-org",
        dest="tooling_org",
        help="Organización donde vive el tooling compartido (uses: y git+https). "
        "Si se omite, vale lo mismo que --org.",
    )
    p.add_argument(
        "--from",
        dest="desde",
        help="Organización de la que se parte. Solo hace falta la primera vez.",
    )
    args = p.parse_args(argv)

    if args.check and (args.org or args.tooling_org):
        p.error("--check no se combina con --org ni --tooling-org.")
    if not args.check and not (args.org or args.tooling_org):
        p.error("usa --check, o --org / --tooling-org.")

    try:
        raiz = resolver_raiz(args.root)
        if args.check:
            return _comprobar(raiz)
        return _aplicar(raiz, args.org, args.tooling_org, args.desde)
    except ErrorDeProyecto as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
