"""Pruebas de `set-github-org`.

Lo que de verdad hay que demostrar no es que sepa reemplazar texto, sino las dos cosas que se
descubrieron rompiendo el CI de un repo real: que NO toque las acciones de terceros (que también
tienen forma `org/repo`), y que `--check` distinga una referencia olvidada de una al día.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lakehouse_tooling.github_org import buscar, main, org_declarada

PYPROJECT = """\
[project]
name = "demo"
version = "0.1.0"

[project.optional-dependencies]
dev = [
    "lakehouse-tooling @ git+https://github.com/vieja-org/databricks-tooling@v1",
    "pytest>=8.0",
]

[tool.lakehouse-tooling]
github_org = "vieja-org"
tables_provider = "demo.contract:listar_tablas"
"""

WORKFLOW = """\
name: ci
on:
  pull_request:
    branches: [main]
jobs:
  validar:
    uses: vieja-org/databricks-tooling/.github/workflows/bundle-ci.yml@v1
    with:
      targets: '["dev"]'
"""

# Un workflow que usa acciones de terceros. Ninguna debe cambiar.
WORKFLOW_CON_ACCIONES = """\
name: otro
on: push
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - uses: databricks/setup-cli@v1.11.0
"""

CODEOWNERS = """\
*                     @vieja-org/plataforma-datos
/contracts/           @vieja-org/equipo-datos
"""


@pytest.fixture
def proyecto(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (tmp_path / "CODEOWNERS").write_text(CODEOWNERS, encoding="utf-8")
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(WORKFLOW, encoding="utf-8")
    (wf / "otro.yml").write_text(WORKFLOW_CON_ACCIONES, encoding="utf-8")
    return tmp_path


def _correr(*args: str, raiz: Path) -> int:
    return main([*args, "--root", str(raiz)])


def test_encuentra_las_cuatro_formas(proyecto: Path) -> None:
    clases = {r.clase for r in buscar(proyecto)}
    assert clases == {
        "uses: de un workflow reutilizable",
        "dependencia git+https",
        "equipo en CODEOWNERS",
        "declaración en pyproject.toml",
    }


def test_no_toca_las_acciones_de_terceros(proyecto: Path) -> None:
    antes = (proyecto / ".github/workflows/otro.yml").read_text(encoding="utf-8")

    assert _correr("--org", "nueva-org", raiz=proyecto) == 0

    despues = (proyecto / ".github/workflows/otro.yml").read_text(encoding="utf-8")
    assert antes == despues
    assert "actions/checkout@v4" in despues
    assert "databricks/setup-cli@v1.11.0" in despues


def test_renombra_las_tres_referencias_y_la_declaracion(proyecto: Path) -> None:
    assert _correr("--org", "nueva-org", raiz=proyecto) == 0

    assert org_declarada(proyecto) == "nueva-org"
    assert "nueva-org/databricks-tooling/.github/workflows/bundle-ci.yml@v1" in (
        proyecto / ".github/workflows/ci.yml"
    ).read_text(encoding="utf-8")
    assert "git+https://github.com/nueva-org/databricks-tooling@v1" in (
        proyecto / "pyproject.toml"
    ).read_text(encoding="utf-8")
    assert "@nueva-org/plataforma-datos" in (proyecto / "CODEOWNERS").read_text(encoding="utf-8")

    assert not [r for r in buscar(proyecto) if r.org == "vieja-org"]


def test_check_pasa_cuando_todo_coincide(proyecto: Path) -> None:
    assert _correr("--check", raiz=proyecto) == 0


def test_check_falla_si_quedo_una_referencia_atras(proyecto: Path) -> None:
    """El caso real: se migró todo menos CODEOWNERS."""
    ruta = proyecto / "CODEOWNERS"
    ruta.write_text(CODEOWNERS.replace("vieja-org", "olvidada"), encoding="utf-8")

    assert _correr("--check", raiz=proyecto) == 1


def test_check_falla_sin_declaracion(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")

    assert _correr("--check", raiz=tmp_path) == 1


def test_primera_adopcion_necesita_from(tmp_path: Path) -> None:
    """Sin declaración previa no hay de dónde partir, y adivinar sería peor que fallar."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    (tmp_path / "CODEOWNERS").write_text("* @sin-declarar/equipo\n", encoding="utf-8")

    assert _correr("--org", "nueva", raiz=tmp_path) == 1

    assert _correr("--org", "nueva", "--from", "sin-declarar", raiz=tmp_path) == 0
    assert org_declarada(tmp_path) == "nueva"
    assert "@nueva/equipo" in (tmp_path / "CODEOWNERS").read_text(encoding="utf-8")


def test_renombrar_a_la_misma_org_no_hace_nada(proyecto: Path) -> None:
    antes = (proyecto / "CODEOWNERS").read_text(encoding="utf-8")

    assert _correr("--org", "vieja-org", raiz=proyecto) == 0

    assert (proyecto / "CODEOWNERS").read_text(encoding="utf-8") == antes


def test_check_y_org_son_excluyentes(proyecto: Path) -> None:
    with pytest.raises(SystemExit):
        _correr("--check", "--org", "otra", raiz=proyecto)
