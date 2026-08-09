"""Mantiene el nombre de la organización de GitHub consistente en todo el repo.

POR QUÉ ESTO EXISTE
GitHub no deja parametrizar la organización donde de verdad importa. El `uses:` de un workflow
reutilizable no admite expresiones (``uses: ${{ vars.ORG }}/repo/...@v1`` es un error de sintaxis,
no una variable sin resolver), pip no expande variables dentro de una dependencia
``git+https://github.com/...``, y CODEOWNERS no resuelve nada: son tres literales que hay que
escribir a mano en tres formatos distintos.

El resultado es que mover un repo de organización —de la de pruebas a la del cliente— es buscar el
nombre en varios archivos y confiar en no haber olvidado ninguno. Cuando se olvida uno, el fallo no
aparece al migrar sino en la primera corrida de CI, con un "workflow was not found" que no menciona
la organización por ningún lado.

Así que la parametrización no puede ser en tiempo de ejecución, pero sí puede ser **verificable**:
la organización se declara una vez en pyproject.toml, este comando la propaga, y ``--check`` en el
CI falla si alguna referencia se quedó atrás.

    [tool.lakehouse-tooling]
    github_org = "mi-organizacion"

Uso:
    set-github-org --check                       # compuerta de CI
    set-github-org --org nueva-org               # renombra desde la org declarada
    set-github-org --org nueva-org --from vieja  # primera adopción, cuando aún no hay declaración
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .proyecto import ErrorDeProyecto, resolver_raiz

# Un nombre de organización o usuario de GitHub: alfanumérico y guiones medios, sin empezar por
# guion. Se captura en el grupo `org` de cada patrón.
_ORG = r"(?P<org>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)"

# Dónde puede aparecer la organización, y con qué forma.
#
# El patrón de `uses:` exige `/.github/workflows/` justamente para NO tocar las acciones de
# terceros: `actions/checkout@v4` y `databricks/setup-cli@v1.11.0` también son `org/repo`, pero
# renombrarlas rompería el CI en vez de migrarlo.
PATRONES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "uses: de un workflow reutilizable",
        re.compile(rf"(?m)^\s*uses:\s*{_ORG}/[\w.-]+/\.github/workflows/"),
    ),
    (
        "dependencia git+https",
        re.compile(rf"git\+https://github\.com/{_ORG}/"),
    ),
    (
        "equipo en CODEOWNERS",
        re.compile(rf"@{_ORG}/[\w.-]+"),
    ),
    (
        "declaración en pyproject.toml",
        re.compile(rf'(?m)^\s*github_org\s*=\s*"{_ORG}"'),
    ),
)

# Archivos que se revisan, relativos a la raíz del repo. CODEOWNERS puede vivir en tres sitios
# según la convención de cada repo, y GitHub los acepta todos.
def _archivos(raiz: Path) -> list[Path]:
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
class Referencia:
    """Una mención de la organización, ubicada para que el mensaje del CI sea accionable."""

    archivo: Path
    linea: int
    org: str
    clase: str
    texto: str

    def ubicacion(self, raiz: Path) -> str:
        return f"{self.archivo.relative_to(raiz)}:{self.linea}"


def org_declarada(raiz: Path) -> str | None:
    """La organización declarada en ``[tool.lakehouse-tooling] github_org`` de pyproject.toml."""
    ruta = raiz / "pyproject.toml"
    if not ruta.is_file():
        return None
    # Se lee con regex y no con tomllib a propósito: el resto del módulo trabaja sobre el texto
    # crudo para preservar comentarios y formato al reescribir, y mezclar las dos lecturas
    # invitaría a que se desincronicen.
    m = re.search(r'(?m)^\s*github_org\s*=\s*"([^"]*)"', ruta.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def buscar(raiz: Path) -> list[Referencia]:
    """Todas las menciones de una organización en el repo, en orden de archivo y línea."""
    encontradas: list[Referencia] = []
    for ruta in _archivos(raiz):
        texto = ruta.read_text(encoding="utf-8")
        for clase, patron in PATRONES:
            for m in patron.finditer(texto):
                encontradas.append(
                    Referencia(
                        archivo=ruta,
                        linea=texto.count("\n", 0, m.start()) + 1,
                        org=m.group("org"),
                        clase=clase,
                        texto=m.group(0).strip(),
                    )
                )
    return sorted(encontradas, key=lambda r: (str(r.archivo), r.linea))


def reescribir(raiz: Path, vieja: str, nueva: str) -> list[Referencia]:
    """Cambia `vieja` por `nueva` en cada referencia. Devuelve las que se tocaron."""
    tocadas: list[Referencia] = []
    for ruta in _archivos(raiz):
        original = ruta.read_text(encoding="utf-8")
        texto = original
        for clase, patron in PATRONES:
            # Las líneas se calculan sobre el texto ORIGINAL: reemplazar un nombre por otro de
            # distinto largo mueve las columnas, nunca las líneas.
            for m in patron.finditer(original):
                if m.group("org") == vieja:
                    tocadas.append(
                        Referencia(
                            archivo=ruta,
                            linea=original.count("\n", 0, m.start()) + 1,
                            org=nueva,
                            clase=clase,
                            texto=m.group(0).strip(),
                        )
                    )

            def _sub(m: re.Match[str]) -> str:
                if m.group("org") != vieja:
                    return m.group(0)
                inicio, fin = m.span("org")
                base = m.start()
                return m.group(0)[: inicio - base] + nueva + m.group(0)[fin - base :]

            texto = patron.sub(_sub, texto)
        if texto != original:
            ruta.write_text(texto, encoding="utf-8")
    return sorted(tocadas, key=lambda r: (str(r.archivo), r.linea))


def _comprobar(raiz: Path) -> int:
    declarada = org_declarada(raiz)
    if declarada is None:
        print(
            "error: no hay organización declarada. Agrega a pyproject.toml:\n\n"
            "    [tool.lakehouse-tooling]\n"
            '    github_org = "<tu-organizacion>"\n\n'
            "Sin ella nada puede comprobar que las referencias de los workflows, la dependencia "
            "del tooling y CODEOWNERS apunten todas al mismo sitio.",
            file=sys.stderr,
        )
        return 1

    referencias = buscar(raiz)
    desviadas = [r for r in referencias if r.org != declarada]

    for r in referencias:
        marca = "  " if r.org == declarada else "->"
        print(f"{marca} {r.ubicacion(raiz):<44} {r.org:<20} {r.clase}")

    if desviadas:
        print(
            f"\n{len(desviadas)} referencia(s) NO apuntan a la organización declarada "
            f"'{declarada}'.\nCorrígelas con:  set-github-org --org {declarada}",
            file=sys.stderr,
        )
        return 1

    print(f"\n{len(referencias)} referencia(s), todas a '{declarada}'.")
    return 0


def _aplicar(raiz: Path, nueva: str, desde: str | None) -> int:
    vieja = desde or org_declarada(raiz)
    if vieja is None:
        print(
            "error: no hay organización declarada de la cual partir. Usa --from para decir qué "
            "nombre se está reemplazando la primera vez.",
            file=sys.stderr,
        )
        return 1

    if vieja == nueva:
        print(f"La organización ya es '{nueva}'. Nada que hacer.")
        return 0

    tocadas = reescribir(raiz, vieja, nueva)

    # La declaración se escribe al final y solo si no existía: si existía, el propio patrón de
    # PATRONES ya la reescribió, y duplicarla dejaría dos valores en desacuerdo.
    if org_declarada(raiz) != nueva:
        ruta = raiz / "pyproject.toml"
        if not ruta.is_file():
            print(
                f"aviso: no hay pyproject.toml en {raiz}, así que la organización no queda "
                "declarada y `--check` no podrá comprobarla.",
                file=sys.stderr,
            )
        else:
            texto = ruta.read_text(encoding="utf-8")
            if "[tool.lakehouse-tooling]" in texto:
                texto = texto.replace(
                    "[tool.lakehouse-tooling]",
                    f'[tool.lakehouse-tooling]\ngithub_org = "{nueva}"',
                    1,
                )
            else:
                seccion = f'[tool.lakehouse-tooling]\ngithub_org = "{nueva}"\n'
                texto = texto.rstrip("\n") + f"\n\n{seccion}"
            ruta.write_text(texto, encoding="utf-8")
            print(f"pyproject.toml: organización declarada como '{nueva}'.")

    for r in tocadas:
        print(f"  {r.ubicacion(raiz):<44} {r.clase}")
    print(f"\n{len(tocadas)} referencia(s) movidas de '{vieja}' a '{nueva}'.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="set-github-org",
        description="Declara y propaga la organización de GitHub del repo.",
    )
    p.add_argument("--root", type=Path, default=None, help="Raíz del repo (default: la del git).")
    p.add_argument("--check", action="store_true", help="Solo verifica. Falla si algo no coincide.")
    p.add_argument("--org", help="Organización destino.")
    p.add_argument(
        "--from",
        dest="desde",
        help="Organización de la que se parte. Solo hace falta la primera vez.",
    )
    args = p.parse_args(argv)

    if args.check == bool(args.org):
        p.error("usa --check o --org, pero no los dos ni ninguno.")

    try:
        raiz = resolver_raiz(args.root)
        return _comprobar(raiz) if args.check else _aplicar(raiz, args.org, args.desde)
    except ErrorDeProyecto as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
