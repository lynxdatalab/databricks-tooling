# databricks-tooling

Workflows reutilizables de GitHub Actions para publicar Databricks Asset Bundles, compartidos entre
todos los repos de lakehouse.

Existe para que el segundo proyecto no empiece copiando y pegando el CI del primero. Cada repo de
capa (`cliente-ejemplo-bronze`, y los de silver/gold cuando toquen) aporta su bundle y su contrato;
la mecánica de autenticar, validar y desplegar vive aquí una sola vez.

---

## Qué hay

| Workflow | Qué hace |
|---|---|
| [`bundle-ci.yml`](.github/workflows/bundle-ci.yml) | Lint de Python, pruebas, checks del repo, análisis de destructivos y `bundle validate`. **No despliega.** Corre en cada PR. |
| [`bundle-analyze.yml`](.github/workflows/bundle-analyze.yml) | Publica **qué va a cambiar** un despliegue. Corre **sin environment** para que el resumen esté visible antes de que qa y prod se detengan a pedir aprobación. |
| [`bundle-deploy.yml`](.github/workflows/bundle-deploy.yml) | Despliega a **un** target. Se encadena una vez por ambiente para la promoción dev → qa → prod. |

## Cómo se usa

```yaml
# .github/workflows/deploy.yml en el repo de la capa
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

El análisis lo hace un script del repo consumidor (`tools/check_destructive.py` en el template),
comparando declaraciones contra la rama base con git. **No necesita credenciales** y es
determinista: dos personas que miren el mismo commit ven lo mismo.

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
| `DATABRICKS_CLIENT_ID` | `terraform output cicd_service_principal_application_ids` en `terraform_databricks/` |

El registro de las federation policies vive en
`cliente-ejemplo-datalake/terraform_databricks/` (variables `github_org` y `github_repos`) y está
documentado en `cliente-ejemplo-datalake/docs/cicd-databricks-oidc.md`.

## Versionado

Tags semánticos (`v1.2.0`) más un tag móvil `v1` que apunta al último de la serie. Los repos
consumidores usan `@v1` y reciben correcciones compatibles sin tocar nada. Un cambio que rompa la
interfaz de entradas sube a `v2`, y cada repo migra cuando pueda.

```bash
git tag -a v1.2.0 -m "..." && git push origin v1.2.0
git tag -f v1 v1.2.0 && git push -f origin v1
```
