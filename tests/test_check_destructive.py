"""Pruebas de la compuerta de cambios destructivos.

Cubren los cuatro casos del docstring del módulo, más el que no estaba cubierto en ninguna copia
anterior de la herramienta: una revisión base irresoluble debe FALLAR, no pasar en verde.
"""

from __future__ import annotations

import sys

import pytest

from lakehouse_tooling.check_destructive import main

from .conftest import RepoFalso


def correr(repo: RepoFalso, *args: str, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(
        sys, "argv", ["check-destructive", "--root", str(repo.raiz), "--base", "HEAD~1", *args]
    )
    return main()


# --------------------------------------------------------------------------------------------
# El caso sano
# --------------------------------------------------------------------------------------------


def test_un_cambio_aditivo_pasa(repo, monkeypatch, capsys):
    repo.escribir("pipelines/silver/ventas.py", "\nimport dlt\n\n# un comentario nuevo\n")
    repo.commit("cambio inocuo")
    assert correr(repo, monkeypatch=monkeypatch) == 0
    assert "✅" in capsys.readouterr().out


# --------------------------------------------------------------------------------------------
# Caso 1 — tablas que dejan de construirse
# --------------------------------------------------------------------------------------------


def test_quitar_una_tabla_del_lock_es_huerfana_y_bloquea(repo, monkeypatch, capsys):
    """El caso por el que existe la herramienta: el CLI de Databricks no dice nada de esto."""
    repo.escribir("tables.lock", "{catalog_silver}.ventas.pedidos\n")
    repo.commit("quitar clientes")

    assert correr(repo, monkeypatch=monkeypatch) == 1
    salida = capsys.readouterr()
    assert "{catalog_silver}.ventas.clientes" in salida.out
    assert "INACTIVA" in salida.out
    assert "acepta-tablas-inactivas" in salida.err


def test_la_etiqueta_desbloquea(repo, monkeypatch):
    repo.escribir("tables.lock", "{catalog_silver}.ventas.pedidos\n")
    repo.commit("quitar clientes")
    assert correr(repo, "--labels", "acepta-tablas-inactivas", monkeypatch=monkeypatch) == 0


# --------------------------------------------------------------------------------------------
# Caso 2 — campos que fuerzan recreación
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("original", "nuevo"),
    [
        ("catalog: ${var.catalog_silver}", "catalog: ${var.catalog_gold}"),
        ("schema: ventas", "schema: ventas_v2"),
        ("serverless: true", "serverless: false"),
    ],
)
def test_cambiar_un_campo_que_recrea_es_destructivo(repo, monkeypatch, capsys, original, nuevo):
    ruta = "resources/pipeline_silver_ventas.yml"
    texto = (repo.raiz / ruta).read_text(encoding="utf-8")
    repo.escribir(ruta, texto.replace(original, nuevo))
    repo.commit("mover el pipeline")

    assert correr(repo, monkeypatch=monkeypatch) == 1
    assert "RECREAR" in capsys.readouterr().out


# --------------------------------------------------------------------------------------------
# Caso 3 — el recurso desaparece
# --------------------------------------------------------------------------------------------


def test_borrar_el_pipeline_es_destructivo_y_sugiere_bind(repo, monkeypatch, capsys):
    repo.borrar("resources/pipeline_silver_ventas.yml")
    repo.commit("borrar el pipeline")

    assert correr(repo, monkeypatch=monkeypatch) == 1
    salida = capsys.readouterr().out
    assert "`silver_ventas` desaparece" in salida
    assert "bundle deployment bind" in salida


def test_borrar_un_job_solo_avisa(repo, monkeypatch, capsys):
    """Un job no guarda datos: bloquear el PR por eso sería ruido."""
    ruta = "resources/pipeline_silver_ventas.yml"
    texto = (repo.raiz / ruta).read_text(encoding="utf-8")
    repo.escribir(ruta, texto.split("  jobs:")[0])
    repo.commit("quitar el job")

    assert correr(repo, monkeypatch=monkeypatch) == 0
    assert "`batch` desaparece" in capsys.readouterr().out


# --------------------------------------------------------------------------------------------
# Caso 4 — retiro deliberado (repos con contrato)
# --------------------------------------------------------------------------------------------


def _con_proveedor_de_retiros(repo: RepoFalso) -> None:
    repo.escribir(
        "pyproject.toml",
        '[project]\nname = "demo"\nversion = "0"\n\n'
        "[tool.lakehouse-tooling]\n"
        'retired_provider = "contrato_retiros:retiradas"\n',
    )
    repo.escribir(
        "src/contrato_retiros.py",
        "def retiradas():\n"
        "    return {'{catalog_silver}.ventas.clientes':"
        " 'a partir de 2026-09-01 — ya no se usa'}\n",
    )


def test_una_tabla_retirada_a_proposito_es_destructiva_no_huerfana(repo, monkeypatch, capsys):
    _con_proveedor_de_retiros(repo)
    repo.escribir("tables.lock", "{catalog_silver}.ventas.pedidos\n")
    repo.commit("retirar clientes")

    assert correr(repo, monkeypatch=monkeypatch) == 1
    salida = capsys.readouterr()
    assert "marcadas para DESTRUIRSE" in salida.out
    assert "ya no se usa" in salida.out
    # Es destrucción deliberada: pide la etiqueta fuerte, no la de tablas inactivas.
    assert "acepta-destruccion" in salida.err
    assert "acepta-tablas-inactivas" not in salida.err


def test_sin_proveedor_una_tabla_quitada_sigue_siendo_huerfana(repo, monkeypatch, capsys):
    """El default prudente: sin forma de saber si fue a propósito, se asume que no."""
    repo.escribir("tables.lock", "{catalog_silver}.ventas.pedidos\n")
    repo.commit("quitar clientes")

    assert correr(repo, monkeypatch=monkeypatch) == 1
    assert "acepta-tablas-inactivas" in capsys.readouterr().err


# --------------------------------------------------------------------------------------------
# Modos y fallos de entorno
# --------------------------------------------------------------------------------------------


def test_modo_report_nunca_falla_y_expone_los_outputs(repo, monkeypatch, tmp_path):
    """Si `analizar` fallara, la corrida no llegaría a los jobs de deploy que debe habilitar."""
    repo.borrar("resources/pipeline_silver_ventas.yml")
    repo.commit("borrar el pipeline")

    salida = tmp_path / "github_output"
    codigo = correr(
        repo, "--mode", "report", "--output-file", str(salida), monkeypatch=monkeypatch
    )
    assert codigo == 0
    assert "destructive=true" in salida.read_text(encoding="utf-8")


def test_una_base_irresoluble_falla_con_codigo_2(repo, monkeypatch, capsys):
    """"No pude ver el antes" NO es "no cambió nada" — es justo el silencio que hay que evitar."""
    monkeypatch.setattr(
        sys, "argv", ["check-destructive", "--root", str(repo.raiz), "--base", "no/existe"]
    )
    assert main() == 2
    assert "fetch-depth: 0" in capsys.readouterr().err


def test_sin_tables_lock_avisa_pero_no_bloquea(repo, monkeypatch, capsys):
    repo.borrar("tables.lock")
    repo.commit("sin lock")
    assert correr(repo, monkeypatch=monkeypatch) == 0
    assert "No hay `tables.lock`" in capsys.readouterr().out
