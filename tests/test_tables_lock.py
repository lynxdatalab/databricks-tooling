"""Pruebas de la generación de `tables.lock`."""

from __future__ import annotations

import sys

import pytest

from lakehouse_tooling.tables_lock import main

from .conftest import RECURSO_EJEMPLO, RepoFalso


def correr(repo: RepoFalso, *args: str, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(sys, "argv", ["gen-tables-lock", "--root", str(repo.raiz), *args])
    return main()


def tablas(repo: RepoFalso) -> list[str]:
    texto = (repo.raiz / "tables.lock").read_text(encoding="utf-8")
    return [ln for ln in texto.splitlines() if ln and not ln.startswith("#")]


def test_cualifica_con_la_variable_de_catalogo_no_con_su_valor(repo, monkeypatch):
    """El nombre resuelto por ambiente daría un lock distinto por target y rompería el diff."""
    assert correr(repo, monkeypatch=monkeypatch) == 0
    assert tablas(repo) == [
        "{catalog_silver}.ventas.clientes",
        "{catalog_silver}.ventas.pedidos",
    ]


def test_check_pasa_cuando_esta_al_dia(repo, monkeypatch, capsys):
    assert correr(repo, "--check", monkeypatch=monkeypatch) == 0
    assert "está al día" in capsys.readouterr().out


def test_check_falla_y_dice_cual_falta(repo, monkeypatch, capsys):
    """El CI debe poder explicar el error sin que nadie regenere y mire el diff."""
    repo.escribir(
        "pipelines/silver/ventas.py",
        '\nimport dlt\n\n@dlt.table(name="pedidos")\ndef pedidos():\n    return 1\n',
    )
    assert correr(repo, "--check", monkeypatch=monkeypatch) == 1
    err = capsys.readouterr().err
    assert "desactualizado" in err
    assert "{catalog_silver}.ventas.clientes" in err


def test_dos_pipelines_sobre_el_mismo_archivo_no_colisionan(repo, monkeypatch):
    """Bronze comparte un solo .py entre áreas; cada una publica a su propio esquema."""
    repo.escribir(
        "resources/pipeline_silver_compras.yml",
        RECURSO_EJEMPLO.replace("silver_ventas:", "silver_compras:").replace(
            "schema: ventas", "schema: compras"
        ),
    )
    assert correr(repo, monkeypatch=monkeypatch) == 0
    assert tablas(repo) == [
        "{catalog_silver}.compras.clientes",
        "{catalog_silver}.compras.pedidos",
        "{catalog_silver}.ventas.clientes",
        "{catalog_silver}.ventas.pedidos",
    ]


def test_capas_distintas_no_colisionan(repo, monkeypatch):
    """La razón de cualificar: `pedidos` existe en bronze y en silver, y no son la misma tabla."""
    repo.escribir(
        "resources/pipeline_bronze_ventas.yml",
        RECURSO_EJEMPLO.replace("silver_ventas:", "bronze_ventas:").replace(
            "${var.catalog_silver}", "${var.catalog_bronze}"
        ),
    )
    assert correr(repo, monkeypatch=monkeypatch) == 0
    assert "{catalog_bronze}.ventas.pedidos" in tablas(repo)
    assert "{catalog_silver}.ventas.pedidos" in tablas(repo)


def test_solo_cuenta_las_tablas_de_pipelines_declarados(repo, monkeypatch):
    """Un .py que ningún recurso carga no se despliega, así que no va al lock."""
    repo.escribir(
        "pipelines/silver/borrador.py",
        '\nimport dlt\n\n@dlt.table(name="jamas_desplegada")\ndef x():\n    return 1\n',
    )
    assert correr(repo, monkeypatch=monkeypatch) == 0
    assert not any("jamas_desplegada" in t for t in tablas(repo))


def test_avisa_de_nombres_dinamicos(repo, monkeypatch, capsys):
    """El caso en que el escaneo es ciego debe gritar, no devolver una lista corta en silencio."""
    repo.escribir(
        "pipelines/silver/ventas.py",
        "\nimport dlt\n\nfor t in ('a', 'b'):\n"
        "    @dlt.table(name=f'tabla_{t}')\n    def _(t=t):\n        return 1\n",
    )
    correr(repo, monkeypatch=monkeypatch)
    err = capsys.readouterr().err
    assert "NO literales" in err
    assert "tables_provider" in err


def test_no_confunde_spark_table_con_una_declaracion(repo, monkeypatch):
    assert correr(repo, monkeypatch=monkeypatch) == 0
    assert not any("origen" in t for t in tablas(repo))


def test_el_proveedor_reemplaza_al_escaneo(repo, monkeypatch):
    """Un repo contract-driven usa esta herramienta en vez de bifurcarla."""
    repo.escribir(
        "pyproject.toml",
        '[project]\nname = "demo"\nversion = "0"\n\n'
        "[tool.lakehouse-tooling]\n"
        'tables_provider = "contrato_lock:listar"\n',
    )
    repo.escribir(
        "src/contrato_lock.py",
        "def listar():\n"
        "    return ['{catalog}.finance.jde_f0101', '{catalog}.finance.jde_f0005']\n",
    )
    assert correr(repo, monkeypatch=monkeypatch) == 0
    assert tablas(repo) == ["{catalog}.finance.jde_f0005", "{catalog}.finance.jde_f0101"]


def test_un_proveedor_roto_falla_en_vez_de_reportar_cero(repo, monkeypatch, capsys):
    """Cero tablas por un import roto le diría al CI que el PR borra el catálogo entero."""
    repo.escribir(
        "pyproject.toml",
        '[project]\nname = "demo"\nversion = "0"\n\n'
        "[tool.lakehouse-tooling]\n"
        'tables_provider = "no_existe:listar"\n',
    )
    assert correr(repo, monkeypatch=monkeypatch) == 2
    assert "No se pudo importar" in capsys.readouterr().err
