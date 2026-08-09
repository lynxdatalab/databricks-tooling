"""Compuertas de CI compartidas por los repos de lakehouse.

Existe para que la lógica que decide **si un cambio destruye datos** tenga una sola copia. Antes
vivía como `tools/*.py` dentro de cada repo, y para cuando había dos consumidores ya había dos
versiones distintas del mismo archivo sin que nadie lo hubiera decidido.

Lo que vive aquí es lo que corre en el CI, sin credenciales y sin cluster:

- :mod:`lakehouse_tooling.tables_lock`       genera y verifica ``tables.lock``
- :mod:`lakehouse_tooling.check_destructive` compara declaraciones contra la rama base

Lo que **no** vive aquí es el código que corre en Databricks (metadatos, configuración de
pipeline). Eso es propio de cada capa —bronze necesita columnas de CDC que silver no— y
unificarlo sería reutilización aparente pagada con un molde que no le queda a nadie.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
