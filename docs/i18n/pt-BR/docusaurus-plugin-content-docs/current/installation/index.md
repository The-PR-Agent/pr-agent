---
title: "Instalação"
sidebar_position: 1
---

Há várias formas de usar o PR-Agent:

<div class="pra-provider-grid">

- <span class="pra-logo pra-logo--terminal" aria-hidden="true"></span> [Localmente](./locally.md)
- <span class="pra-logo pra-logo--github" aria-hidden="true"></span> [GitHub](./github.md)
- <span class="pra-logo pra-logo--gitlab" aria-hidden="true"></span> [GitLab](./gitlab.md)
- <span class="pra-logo pra-logo--bitbucket" aria-hidden="true"></span> [Bitbucket](./bitbucket.md)
- <span class="pra-logo pra-logo--azuredevops" aria-hidden="true"></span> [Azure DevOps](./azure.md)
- <span class="pra-logo pra-logo--gitea" aria-hidden="true"></span> [Gitea](./gitea.md)

</div>

## Requisições HTTP do polling do GitHub

Quando o polling do GitHub precisa olhar para trás nos comentários de um PR, ele
reaproveita a sessão HTTP do polling em vez de bloquear o event loop com uma
requisição síncrona. O `github.polling_request_timeout` define o tempo limite
total de toda a varredura de histórico de comentários do fallback, incluindo
quaisquer requisições de páginas seguintes (padrão: 10 segundos; valores
positivos são limitados a 60). Valores inválidos usam o padrão e emitem um
aviso. Configure na configuração do host ou por `GITHUB__POLLING_REQUEST_TIMEOUT`;
configurações de repositório não controlam esse limite. Isso não altera as
outras requisições de polling nem a varredura sequencial de notificações. O
fallback agora segue o comportamento de proxy e TLS da sessão aiohttp existente,
e não configurações de ambiente específicas da Requests.

O polling do GitHub só processa notificações de menção vindas de repositórios
listados em `github.polling_repositories` (por exemplo, `["org/repo"]`). A lista
é exclusiva do host; configurações de repositório e argumentos em comentários não
podem ampliá-la. O padrão vazio ignora toda notificação de polling e ainda assim
a marca como lida, então configure-a na configuração do host ou por
`GITHUB__POLLING_REPOSITORIES` antes de habilitar o polling ou atualizar.

O comando que vem depois da primeira menção à conta de polling é preservado
intacto, incluindo menções posteriores em perguntas simples ou entre aspas
duplas. Substitua `@your-bot` pelo nome de usuário da conta de polling:

```text
@your-bot /ask Why does @your-bot appear in this workflow?
@your-bot /ask "Why does @your-bot appear in this workflow?"
```

## Dimensionando um servidor de webhook auto-hospedado

Os servidores de webhook do GitHub, GitLab, Gitea e Bitbucket Server (os alvos Docker `github_app`, `gitlab_webhook`, `gitea_app` e `bitbucket_server_webhook`) rodam sob gunicorn com vários processos worker, para que um worker ocupado atendendo a uma requisição não consiga bloquear o health check servido por outro. As demais implantações — Bitbucket Cloud, Azure DevOps, polling do GitHub e as variantes Lambda — não usam essa configuração de workers do gunicorn.

| Variável               | Padrão          | Descrição                                                                                      |
|------------------------|-----------------|------------------------------------------------------------------------------------------------|
| `GUNICORN_WORKERS`     | *(não definida)* | Fixa exatamente a quantidade de workers. Prevalece sobre a contagem derivada e sobre `GUNICORN_MAX_WORKERS`. |
| `GUNICORN_MAX_WORKERS` | `4`             | Limite superior da contagem de workers derivada automaticamente.                                |
| `PORT`                 | `3000`          | Porta em que o servidor escuta.                                                                 |

Quando `GUNICORN_WORKERS` não está definida, a contagem de workers é derivada das CPUs de fato disponíveis para o contêiner (limite de CPU do cgroup, caindo para a afinidade de CPU), restrita entre 2 e `GUNICORN_MAX_WORKERS`. Note que um *request* de CPU sem um *limit* não deixa cota de cgroup para ler, então é o teto que limita a contagem de workers nesse caso.

**Dimensionando memória:** importar a aplicação custa por volta de 250 MB. A aplicação é importada uma vez no master do gunicorn e os workers são bifurcados a partir dele, então eles compartilham a maior parte dessa base por copy-on-write em vez de cada um pagar o custo inteiro — a memória total cresce com a quantidade de workers, mas bem menos que 250 MB por worker. Comece com cerca de 1 Gi para o padrão de 4 workers e ajuste conforme as suas próprias métricas.

Se o contêiner for encerrado por OOM durante a inicialização, reduza a quantidade de workers:

```bash
GUNICORN_WORKERS=2
```

## Workers do polling do GitHub

O polling do GitHub usa um processo de polling e um processo filho separado para
cada comentário aceito. No máximo 10 desses workers filhos podem estar ativos
somando todas as iterações de polling. Se um lote anterior ainda estiver ocupando
o limite, o trabalho aceito espera por uma vaga livre sem bloquear o event loop.
Isso pode atrasar o próximo polling de notificações sob carga sustentada.

O limite por lote já existente não mudou: apenas os 10 primeiros comentários na
fila de um lote são aceitos; o excedente é registrado em log e descartado. O
trabalho em espera é mantido apenas em memória. A confirmação de notificações não
mudou, então falhas de encerramento ou de inicialização não garantem reentrega.
Interrupções de despacho registram a contagem restante. Uma falha na inicialização
de um worker interrompe o polling em vez de arriscar mais despachos com um filho
não rastreado; investigue a falha antes de reiniciar. Filhos concluídos são unidos
sem espera e fechados a cada iteração; filhos vivos não são encerrados no
cancelamento. Isso não é uma fila durável, nem um prazo para chamadas de modelo,
nem um limite de memória do host.

## Nomes de imagem Docker e tags de versão

:::note[Migração de namespace no Docker Hub]
As versões **`0.34.2` e posteriores** são publicadas em [`pragent/pr-agent`](https://hub.docker.com/r/pragent/pr-agent). As versões antigas (até a `v0.31`, inclusive) continuam no namespace legado [`codiumai/pr-agent`](https://hub.docker.com/r/codiumai/pr-agent) como um arquivo congelado — nenhuma imagem nova é publicada lá. Os exemplos deste site referenciam o namespace novo; se você estiver fixando uma versão anterior à `0.34.2`, troque `pragent/pr-agent` por `codiumai/pr-agent` nas suas referências de `image:` / `docker pull` / `uses: docker://`.
:::

:::note[Releases imutáveis e tags de versão]
**O que você fixa é o que você recebe.** Artefatos com número de versão nunca podem mudar depois de publicados:

- **Releases do GitHub** — a tag do Git não pode ser movida nem apagada, e os arquivos anexados não podem ser adicionados, substituídos nem removidos. A proteção sobrevive até mesmo à exclusão do repositório, então uma tag de um release imutável nunca pode ser reutilizada por um repositório recriado com o mesmo nome. (Títulos e notas de release continuam editáveis; a imutabilidade cobre a tag e os arquivos.)
- **Imagens Docker** — tags de versão como `0.40.0` e `0.40.0-github_app` sempre resolvem para a mesma imagem. Uma vez publicadas, não podem ser sobrescritas nem reapontadas. Apenas a imagem `cli` instala o script de console `pr-agent`; nas imagens de serviço, rode `python -m pr_agent.cli …` (ou `python /app/pr_agent/cli.py …`) — veja [Health Check da CLI](../usage-guide/automations_and_usage.md#cli-health-check).

**Tags móveis continuam mutáveis por design.** `latest`, `github_action`, `github_lambda`, `gitlab_lambda`, `gitlab_webhook`, `gitea_app`, `mosaico_agent` e `bitbucket_server_webhook` passam a apontar para o build mais recente a cada release. Elas são convenientes para experimentar, mas um `docker pull` da mesma tag móvel em dois dias diferentes pode te entregar duas imagens diferentes.

Para qualquer coisa de que você dependa — CI, webhooks em produção, passos de Action fixados — referencie uma tag de versão (ou um digest) em vez de uma móvel. Atualizar passa então a ser uma mudança deliberada que você faz, e não algo que acontece por baixo dos panos.
:::
