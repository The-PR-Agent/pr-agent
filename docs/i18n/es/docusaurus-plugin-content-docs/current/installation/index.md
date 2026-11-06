---
title: "Instalación"
sidebar_position: 1
---

Hay varias formas de usar PR-Agent:

<div class="pra-provider-grid">

- <span class="pra-logo pra-logo--terminal" aria-hidden="true"></span> [En local](./locally.md)
- <span class="pra-logo pra-logo--github" aria-hidden="true"></span> [GitHub](./github.md)
- <span class="pra-logo pra-logo--gitlab" aria-hidden="true"></span> [GitLab](./gitlab.md)
- <span class="pra-logo pra-logo--bitbucket" aria-hidden="true"></span> [Bitbucket](./bitbucket.md)
- <span class="pra-logo pra-logo--azuredevops" aria-hidden="true"></span> [Azure DevOps](./azure.md)
- <span class="pra-logo pra-logo--gitea" aria-hidden="true"></span> [Gitea](./gitea.md)

</div>

## Peticiones HTTP del sondeo de GitHub

Cuando el sondeo de GitHub necesita mirar hacia atrás en los comentarios de un PR,
reutiliza la sesión HTTP del sondeo en lugar de bloquear el bucle de eventos con
una petición síncrona. `github.polling_request_timeout` fija el tiempo de espera
total de todo el recorrido del historial de comentarios del respaldo, incluidas
las peticiones de páginas posteriores (por defecto: 10 segundos; los valores
positivos se limitan a 60). Los valores no válidos usan el valor por defecto y
emiten un aviso. Configúralo en la configuración del host o mediante
`GITHUB__POLLING_REQUEST_TIMEOUT`; los ajustes del repositorio no controlan este
límite. Esto no cambia las demás peticiones de sondeo ni el recorrido secuencial
de notificaciones. El respaldo sigue ahora el comportamiento de proxy y TLS de la
sesión aiohttp existente, no los ajustes de entorno propios de Requests.

El sondeo de GitHub solo procesa notificaciones de mención procedentes de los
repositorios listados en `github.polling_repositories` (por ejemplo,
`["org/repo"]`). La lista es exclusiva del host; ni los ajustes del repositorio ni
los argumentos en comentarios pueden ampliarla. El valor por defecto vacío ignora
todas las notificaciones de sondeo y aun así las marca como leídas, así que
configúrala en la configuración del host o mediante `GITHUB__POLLING_REPOSITORIES`
antes de habilitar el sondeo o de actualizar.

El comando que sigue a la primera mención de la cuenta de sondeo se conserva
intacto, incluidas las menciones posteriores en preguntas simples o entre comillas
dobles. Sustituye `@your-bot` por el nombre de usuario de la cuenta de sondeo:

```text
@your-bot /ask Why does @your-bot appear in this workflow?
@your-bot /ask "Why does @your-bot appear in this workflow?"
```

## Dimensionar un servidor de webhooks autoalojado

Los servidores de webhooks de GitHub, GitLab, Gitea y Bitbucket Server (los objetivos de Docker `github_app`, `gitlab_webhook`, `gitea_app` y `bitbucket_server_webhook`) se ejecutan bajo gunicorn con varios procesos de trabajo, de modo que un worker ocupado atendiendo una petición no pueda bloquear la comprobación de estado que sirve otro. El resto de despliegues —Bitbucket Cloud, Azure DevOps, el sondeo de GitHub y las variantes Lambda— no usan esta configuración de workers de gunicorn.

| Variable               | Por defecto      | Descripción                                                                                      |
|------------------------|------------------|--------------------------------------------------------------------------------------------------|
| `GUNICORN_WORKERS`     | *(sin definir)*  | Fija exactamente el número de workers. Prevalece sobre el número derivado y sobre `GUNICORN_MAX_WORKERS`. |
| `GUNICORN_MAX_WORKERS` | `4`              | Límite superior del número de workers derivado automáticamente.                                   |
| `PORT`                 | `3000`           | Puerto en el que escucha el servidor.                                                             |

Cuando `GUNICORN_WORKERS` no está definida, el número de workers se deriva de las CPU realmente disponibles para el contenedor (límite de CPU del cgroup, recurriendo a la afinidad de CPU), acotado entre 2 y `GUNICORN_MAX_WORKERS`. Ten en cuenta que un *request* de CPU sin un *limit* no deja cuota de cgroup que leer, así que ahí es el tope lo que acota el número de workers.

**Dimensionar la memoria:** importar la aplicación cuesta unos 250 MB. La aplicación se importa una sola vez en el master de gunicorn y los workers se bifurcan a partir de él, así que comparten la mayor parte de esa base por copy-on-write en vez de pagarla entera cada uno: la memoria total crece con el número de workers, pero bastante menos de 250 MB por worker. Empieza por alrededor de 1 Gi para los 4 workers por defecto y ajusta según tus propias métricas.

Si el contenedor termina por falta de memoria (OOMKilled) durante el arranque, baja el número de workers:

```bash
GUNICORN_WORKERS=2
```

## Workers del sondeo de GitHub

El sondeo de GitHub usa un proceso de sondeo y un proceso hijo aparte para cada
comentario aceptado. Como mucho puede haber 10 de estos workers hijos activos
sumando todas las iteraciones de sondeo. Si un lote anterior sigue ocupando el
límite, el trabajo aceptado espera a que quede un hueco libre sin bloquear el
bucle de eventos. Esto puede retrasar el siguiente sondeo de notificaciones con
carga sostenida.

El límite por lote que ya existía no cambia: solo se aceptan los 10 primeros
comentarios en cola de un lote; el trabajo sobrante se registra y se descarta. El
trabajo en espera se mantiene solo en memoria. La confirmación de notificaciones
no cambia, así que los fallos de apagado o de arranque no garantizan una nueva
entrega. Las interrupciones del reparto registran el número restante. Un fallo al
arrancar un worker detiene el sondeo en lugar de arriesgar más repartos con un
hijo sin seguimiento; revisa el fallo antes de reiniciar. Los hijos terminados se
unen sin esperar y se cierran en cada iteración; los hijos vivos no se terminan al
cancelar. Esto no es una cola duradera, ni un plazo para las llamadas al modelo,
ni un límite de memoria para todo el host.

## Nombres de imagen de Docker y etiquetas de versión

:::note[Migración de namespace en Docker Hub]
Las versiones **`0.34.2` y posteriores** se publican en [`pragent/pr-agent`](https://hub.docker.com/r/pragent/pr-agent). Las versiones antiguas (hasta la `v0.31` incluida) siguen en el namespace heredado [`codiumai/pr-agent`](https://hub.docker.com/r/codiumai/pr-agent) como archivo congelado: allí no se publican imágenes nuevas. Los ejemplos de este sitio usan el namespace nuevo; si estás fijando una versión anterior a la `0.34.2`, cambia `pragent/pr-agent` por `codiumai/pr-agent` en tus referencias de `image:` / `docker pull` / `uses: docker://`.
:::

:::note[Versiones inmutables y etiquetas de versión]
**Lo que fijas es lo que obtienes.** Los artefactos con número de versión no pueden cambiar nunca una vez publicados:

- **Versiones de GitHub** — la etiqueta de Git no se puede mover ni borrar, y los archivos adjuntos no se pueden añadir, sustituir ni eliminar. La protección sobrevive incluso al borrado del repositorio, así que la etiqueta de una versión inmutable nunca podrá reutilizarla un repositorio recreado con el mismo nombre. (Los títulos y las notas de la versión siguen siendo editables; la inmutabilidad cubre la etiqueta y los archivos.)
- **Imágenes de Docker** — las etiquetas de versión como `0.40.0` y `0.40.0-github_app` siempre resuelven a la misma imagen. Una vez subidas, no se pueden sobrescribir ni reapuntar. Solo la imagen `cli` instala el script de consola `pr-agent`; en las imágenes de servicio ejecuta `python -m pr_agent.cli …` (o `python /app/pr_agent/cli.py …`) — consulta [Comprobación de estado de la CLI](../usage-guide/automations_and_usage.md#cli-health-check).

**Las etiquetas móviles siguen siendo mutables por diseño.** `latest`, `github_action`, `github_lambda`, `gitlab_lambda`, `gitlab_webhook`, `gitea_app`, `mosaico_agent` y `bitbucket_server_webhook` pasan a apuntar a la compilación más reciente en cada versión. Son cómodas para hacer pruebas, pero un `docker pull` de la misma etiqueta móvil en dos días distintos puede darte dos imágenes distintas.

Para cualquier cosa de la que dependas —CI, webhooks en producción, pasos de Action fijados— usa una etiqueta de versión (o un digest) en lugar de una móvil. Actualizar se convierte entonces en un cambio deliberado que haces tú, no en algo que ocurre por debajo.
:::
