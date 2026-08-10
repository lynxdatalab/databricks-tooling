"""Pruebas de `check-schemas`.

Lo que hay que demostrar no es que sepa comparar cadenas, sino que atrapa los dos casos que los
grants NO pueden atrapar: un proyecto que materializa dentro del esquema de otro, y —peor— uno que
declara el RETIRO de una tabla ajena, que no la deja inactiva sino que la borra.
"""

from __future__ import annotations

from lakehouse_tooling.esquemas import main

from .conftest import RepoFalso

PYPROJECT = """\
[project]
name = "demo"
version = "0.1.0"

[tool.lakehouse-tooling]
{extra}
"""


def _con_config(repo: RepoFalso, extra: str) -> None:
    repo.escribir("pyproject.toml", PYPROJECT.format(extra=extra))


def _correr(repo: RepoFalso) -> int:
    return main(["--root", str(repo.raiz)])


def test_sin_declaracion_se_omite(repo: RepoFalso) -> None:
    """Un repo que no declara sus esquemas no está diciendo 'todos': no lo ha pensado."""
    _con_config(repo, 'tables_provider = ""')

    assert _correr(repo) == 0


def test_todo_dentro_del_esquema(repo: RepoFalso) -> None:
    _con_config(repo, 'allowed_schemas = ["ventas"]')

    assert _correr(repo) == 0


def test_atrapa_una_tabla_en_esquema_ajeno(repo: RepoFalso, capsys) -> None:
    """El caso que los grants no cortan: el pipeline escribe con la identidad del SP."""
    _con_config(repo, 'allowed_schemas = ["mi_proyecto"]')

    assert _correr(repo) == 1

    err = capsys.readouterr().err
    assert "ventas" in err
    assert "mi_proyecto" in err


def test_atrapa_un_retiro_en_esquema_ajeno(repo: RepoFalso, capsys) -> None:
    """El peor caso: `retire:` no deja inactiva la tabla, hace que el job la BORRE."""
    repo.escribir(
        "src/esq_retiros.py",
        "def listar_esquemas():\n"
        "    return ['ventas']\n"
        "\n"
        "def listar_retiradas():\n"
        "    return {'{catalog}.finanzas.saldos': 'ya no se usa'}\n",
    )
    _con_config(
        repo,
        'schemas_provider = "esq_retiros:listar_esquemas"\n'
        'retired_provider = "esq_retiros:listar_retiradas"',
    )

    assert _correr(repo) == 1

    err = capsys.readouterr().err
    assert "finanzas" in err
    assert "RETIRA" in err, "un retiro fuera del esquema propio tiene que distinguirse de un alta"


def test_el_proveedor_dinamico_funciona(repo: RepoFalso) -> None:
    """Un repo multi-área declara sus esquemas desde su contrato, no a mano."""
    repo.escribir(
        "src/esq_dinamico.py", "def listar_esquemas():\n    return ['ventas', 'finanzas']\n"
    )
    _con_config(repo, 'schemas_provider = "esq_dinamico:listar_esquemas"')

    assert _correr(repo) == 0


def test_allowed_schemas_mal_formado(repo: RepoFalso) -> None:
    _con_config(repo, 'allowed_schemas = "ventas"')

    assert _correr(repo) == 2


def test_un_nombre_sin_esquema_falla_explicito(repo: RepoFalso, capsys) -> None:
    """Si no se puede saber a qué esquema va, no se puede afirmar que está dentro."""
    repo.escribir("src/esq_sin_forma.py", "def listar_tablas():\n    return ['solo_la_tabla']\n")
    _con_config(
        repo,
        'allowed_schemas = ["ventas"]\ntables_provider = "esq_sin_forma:listar_tablas"',
    )

    assert _correr(repo) == 1
    assert "catalogo.esquema.tabla" in capsys.readouterr().err


def test_la_raiz_se_resuelve_sola(repo: RepoFalso, monkeypatch) -> None:
    """Sin --root usa la raíz del git, que es como corre en el CI."""
    _con_config(repo, 'allowed_schemas = ["ventas"]')
    monkeypatch.chdir(repo.raiz)

    assert main([]) == 0


def test_no_confunde_dos_esquemas_con_prefijo_comun(repo: RepoFalso) -> None:
    """`ventas` no autoriza `ventas_historico`: la comparación es exacta, no por prefijo."""
    repo.escribir(
        "src/esq_prefijo.py", "def listar_tablas():\n    return ['{c}.ventas_historico.t']\n"
    )
    _con_config(
        repo,
        'allowed_schemas = ["ventas"]\ntables_provider = "esq_prefijo:listar_tablas"',
    )

    assert _correr(repo) == 1
