# databricks-tooling

Las compuertas de CI/CD compartidas por todos los repos de lakehouse: los workflows reutilizables de
GitHub Actions y el paquete de Python que ejecutan.

Existe para que el segundo proyecto no empiece copiando y pegando el CI del primero. Cada repo
aporta su bundle y sus tablas; la mecánica de autenticar, validar, detectar cambios destructivos y
desplegar vive aquí una sola vez.

> **Por qué el paquete y no solo los workflows.** Al principio aquí solo vivían los tres YAML, y la
> lógica —`check_destructive.py`, `gen_tables_lock.py`— se copiaba a cada repo. Con dos consumidores
> ya había dos versiones distintas del mismo archivo, con mejoras en una que la otra nunca recibió.
> Un arreglo a la compuerta había que aplicarlo N veces a mano. Lo que se comparte, se versiona.

---

## Qué hay

### Workflows reutilizables

| Workflow | Qué hace |
|---|---|
| [`bundle-ci.yml`](.github/workflows/bundle-ci.yml) | Lint de Python, pruebas, checks del repo, análisis de destructivos y `bundle validate`. **No despliega.** Corre en cada PR. |
| [`bundle-analyze.yml`](.github/workflows/bundle-analyze.yml) | Publica **qué va a cambiar** un despliegue. Corre **sin environment** para que el resumen esté visible antes de que qa y prod se detengan a pedir aprobación. |
| [`bundle-deploy.yml`](.github/workflows/bundle-deploy.yml) | Despliega a **un** target. Se encadena una vez por ambiente para la promoción dev → qa → prod. |

### Paquete `lakehouse-tooling`

| Ejecutable | Qué hace |
|---|---|
| `check-destructive` | Compara declaraciones contra otra revisión de git y reporta lo que destruye datos. Es la compuerta. |
| `gen-tables-lock` | Genera y verifica `tables.lock`, el inventario versionado de las tablas que el repo materializa. |
| `set-github-org` | Declara y propaga la organización de GitHub del repo, y verifica que nadie se quedó atrás. |
| `check-schemas` | Falla si el repo declara tablas —o retiros— fuera de los esquemas que le corresponden. |

Ninguno necesita credenciales ni cluster: leen archivos del repo y el historial de git.

#### `set-github-org`, o cómo se mueve un repo de organización

GitHub no deja parametrizar la organización donde importa. El `uses:` de un workflow reutilizable
no admite expresiones —`uses: ${{ vars.ORG }}/repo/...@v1` es un error de sintaxis, no una variable
sin resolver—, pip no expande variables dentro de un `git+https://github.com/...`, y CODEOWNERS no
resuelve nada. Son tres literales, en tres formatos distintos.

**Y no todos apuntan al mismo sitio.** El tooling compartido vive en la organización del proveedor
y los repos de proyecto en la del cliente, que es el caso normal y no la excepción:

| | Rige | Qué es |
|---|---|---|
| `github_org` | los equipos de `@org/equipo` en CODEOWNERS | quién es **dueño** de este repo |
| `tooling_org` | el `uses:` y la dependencia `git+https` | dónde vive el **tooling** compartido |

Migrar un repo de organización mueve lo primero y **no debe tocar lo segundo**. Confundirlos deja
el CI con `workflow was not found`, un error que no menciona la organización por ningún lado.

```toml
[tool.lakehouse-tooling]
github_org   = "org-del-cliente"
tooling_org  = "org-del-proveedor"    # opcional
tooling_repo = "databricks-tooling"   # opcional
```

```bash
set-github-org --check                                   # compuerta de CI
set-github-org --org nueva-org                           # mueve el repo de organización
set-github-org --org cliente --tooling-org proveedor     # los separa
set-github-org --org nueva --from vieja                  # primera adopción
```

**`tooling_org` ausente equivale a `github_org`**, y por eso `--org nueva` a secas mueve las dos
cosas: es como se comportaban los repos escritos cuando solo había una organización, y sin ese
default mover el tag `v1` los pondría en rojo. Separarlas hay que pedirlo explícitamente.

No toca las acciones de terceros —`actions/checkout@v4` y `databricks/setup-cli@v1.11.0` también
tienen forma `org/repo`— ni los workflows reutilizables de otro repo: la clasificación es por
**repo referenciado**, no por la forma de la referencia.

**`docs/` queda deliberadamente fuera.** Una bitácora que dice "esto se desplegó en tal
organización" es un hecho histórico; reescribirla la convertiría en una mentira. Las referencias en
documentación se actualizan a mano, agregando, no sustituyendo.

## Cómo se usa

En el `pyproject.toml` del repo consumidor:

```toml
[project.optional-dependencies]
dev = [
    "lakehouse-tooling @ git+https://github.com/<org>/databricks-tooling@v1",
    "pytest>=8.0",
    "ruff>=0.6",
]
```

En sus workflows:

```yaml
# .github/workflows/deploy.yml
permissions:
  id-token: write     # ← OBLIGATORIO, y va en el LLAMADOR
  contents: read

jobs:
  # Sin environment: corre de inmediato y deja el análisis en el resumen de la corrida.
  analizar:
    uses: <org>/databricks-tooling/.github/workflows/bundle-analyze.yml@v1

  dev:
    needs: analizar
    uses: <org>/databricks-tooling/.github/workflows/bundle-deploy.yml@v1
    permissions: { id-token: write, contents: read }
    with:
      target: dev
      # Fijo en false en el push a main: nadie destruye datos por hacer merge.
      allow-destructive: ${{ github.event_name == 'workflow_dispatch' && inputs.allow-destructive || false }}

  qa:
    needs: dev
    uses: <org>/databricks-tooling/.github/workflows/bundle-deploy.yml@v1
    permissions: { id-token: write, contents: read }
    with: { target: qa }
```

#### `check-schemas`, o por qué los grants no bastan

Los grants acotan lo que puede escribir una **persona**, no lo que escribe su **código**. El
pipeline no corre con la identidad de quien lo programó: corre con la del service principal, que
tiene permisos sobre todo el catálogo de su ambiente. Es un *confused deputy* — al desarrollador no
le hacen falta permisos, le basta con que el SP los tenga.

Así que un repo puede materializar una tabla dentro del esquema de otro proyecto sin encontrarse
ningún obstáculo, y sin que nadie se entere hasta que aparece en el catálogo equivocado.

```toml
[tool.lakehouse-tooling]
allowed_schemas  = ["ventas"]                             # lista literal
schemas_provider = "mipaquete.contract:listar_esquemas"   # o dinámico, para repos multi-área
```

Sin ninguna de las dos la comprobación se omite: un repo que no declara sus esquemas no está
diciendo "todos", está diciendo "todavía no lo he pensado".

**También mira los retiros**, y es la parte que más importa: un `retire:` sobre una tabla ajena no
la deja inactiva, hace que el job la **borre**. Es la única operación del sistema que destruye
datos y sería la más fácil de dirigir al sitio equivocado.

**Lo que no cubre:** es una comprobación sobre lo DECLARADO. Un nombre construido en tiempo de
ejecución se le escapa, y sin revisión obligatoria de PR toda compuerta a nivel de código es un
aviso y no un muro. Lo único no evadible son los grants de la identidad que ejecuta.

## `tables.lock`, y el cambio que nadie detecta

Quitar una tabla de un pipeline **no le pide permiso al CLI de Databricks**. El recurso `pipeline`
no cambió, así que `bundle deploy` no dice nada: la tabla queda inactiva —consultable, sin
actualizarse— y nadie se entera. Es el cambio destructivo más frecuente y el único sin alarma
nativa.

`tables.lock` es el artefacto que lo hace visible: generado, versionado, y comparado contra la rama
base en cada PR. Las tablas van cualificadas y **sin resolver por ambiente**:

```text
{catalog_bronze}.ventas.pedidos
{catalog_silver}.ventas.pedidos
```

El catálogo queda como el nombre de la variable del bundle, no como su valor. Con el nombre real
habría un archivo distinto por ambiente y el diff dejaría de significar "qué tablas quita este PR".
De paso, en un repo multi-capa el placeholder desambigua: `pedidos` en bronze y `pedidos` en silver
no son la misma tabla.

### Repos que generan sus tablas desde un contrato

El descubrimiento por defecto analiza el código buscando `@dlt.table(name="...")` con nombre
literal, y no ve nombres construidos en tiempo de ejecución. Un repo que declara sus tablas en un
contrato y las materializa en un bucle —lo correcto a escala— declara de dónde sacarlas:

```toml
[tool.lakehouse-tooling]
tables_provider  = "mipaquete.contract:listar_tablas"    # -> Iterable[str] cualificados
retired_provider = "mipaquete.contract:listar_retiradas" # -> dict[nombre, motivo]
```

`retired_provider` es lo que permite distinguir una tabla **retirada a propósito** (destrucción
deliberada → pide `acepta-destruccion`) de una que **se cayó del pipeline por descuido** (→ pide
`acepta-tablas-inactivas`). Sin él, ambas se reportan como huérfanas, que es el default prudente.

Ambos son opcionales. Un proveedor que truena **falla la corrida** en vez de reportar cero tablas:
cero por un import roto le diría al CI que el PR borra el catálogo entero, o peor, que no borra nada.

## Acciones destructivas: cerrado por default

`bundle deploy` **nunca** pasa `--auto-approve` salvo que alguien lo autorice explícitamente. Si el
cambio recrea o elimina un pipeline —lo que borra sus materialized views y streaming tables— el CLI
intenta pedir confirmación, en CI no hay terminal que responda, y el job falla. Ese fail-closed es
el diseño, y un paso posterior traduce el error en instrucciones concretas.

Para autorizarlo hace falta un `workflow_dispatch` con `allow-destructive`. Entonces el job de
deploy **cambia su propio nombre** a `deploy qa ⚠️ DESTRUCTIVO`, que es lo que GitHub muestra en el
diálogo de aprobación sin que el revisor abra nada.

> **Por qué `bundle-analyze.yml` es un job aparte y no un paso del deploy:** GitHub evalúa las
> protection rules del environment **antes** de ejecutar el primer paso del job. Un job con
> `environment: qa` se detiene sin alcanzar a imprimir nada, así que el revisor aprobaría a ciegas.

### Tres cosas que suelen morder

1. **`id-token: write` lo otorga el llamador.** Declararlo dentro del workflow reutilizable no
   sirve: los permisos del token se resuelven en el workflow de entrada. Sin él, la CLI no
   consigue token OIDC y falla con un error de credenciales que no menciona los permisos.

2. **Repo privado ⇒ hay que habilitar el acceso.** En este repo:
   *Settings → Actions → General → Access →* **Accessible from repositories in the organization**.
   Sin eso, el llamador falla con "workflow was not found".

3. **Fijar la versión con el tag `v1`**, no con `@main`. Un cambio aquí llegaría a todos los repos
   en su siguiente deploy, incluido el de producción de un proyecto que nadie está mirando.

## Autenticación: sin secretos de larga vida

No hay ningún `client_secret` en ningún repo. En cada corrida:

1. GitHub emite un token OIDC efímero con el claim
   `sub = repo:{org}/{repo}:environment:{target}`.
2. La *federation policy* del service principal de ese ambiente en Databricks lo canjea por acceso.
3. El token muere con el job.

GitHub solo emite ese `sub` cuando el job declara `environment: {target}` — y para `prod` eso exige
pasar las protection rules del environment. Las tres capas (protection rules, identidad por
ambiente, grants de Unity Catalog por ambiente) son independientes: saltarse una no alcanza.

Lo que cada GitHub Environment necesita, como **variables** (no secrets — ninguno de los dos lo es):

| Variable | De dónde sale |
|---|---|
| `DATABRICKS_HOST` | URL del workspace |
| `DATABRICKS_CLIENT_ID` | Application ID del service principal de CI/CD de ese ambiente |

El registro de las federation policies se hace en el Terraform del lakehouse (una policy por
combinación repo × ambiente). El paso a paso, con el ejemplo de punta a punta, está en el
`GETTING-STARTED.md` del repo template.

## Desarrollo

```bash
pip install -e '.[dev]'
ruff check . && pytest -q
```

Las pruebas construyen repos git temporales con historia real: las compuertas comparan contra una
revisión base, y un mock de git dejaría sin probar justo la parte que falla en producción.

## Versionado

Tags semánticos (`v1.2.0`) más un tag móvil `v1` que apunta al último de la serie. Los repos
consumidores usan `@v1` y reciben correcciones compatibles sin tocar nada. Un cambio que rompa la
interfaz de entradas de los workflows —o la de los ejecutables— sube a `v2`, y cada repo migra
cuando pueda.

```bash
git tag -a v1.2.0 -m "..." && git push origin v1.2.0
git tag -f v1 v1.2.0 && git push -f origin v1
```

> **El tag `v1` tiene que existir antes que cualquier consumidor.** Un `uses: ...@v1` contra un tag
> inexistente falla con "workflow was not found", que se lee como un problema de permisos y no como
> lo que es. Publicarlo es el primer paso, no el último.
