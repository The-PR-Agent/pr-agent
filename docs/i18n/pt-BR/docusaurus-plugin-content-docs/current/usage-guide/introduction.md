---
title: "Introdução"
sidebar_position: 2
---

Depois da [instalação](../installation/index.md), há três formas básicas de acionar o PR-Agent:

1. Rodando um comando da CLI localmente
2. Uso online — <a href="https://github.com/the-pr-agent/pr-agent/pull/229#issuecomment-1695021901" target="_blank" rel="noopener noreferrer">comentando</a> em um PR
3. Habilitando as ferramentas do PR-Agent para rodarem automaticamente quando um novo PR é aberto

Mais especificamente, os comandos da CLI podem ser disparados invocando uma [imagem Docker](../installation/locally.md#using-docker-image) pré-construída ou um [repositório clonado localmente](../installation/locally.md#run-from-source).

Para o uso online, você vai precisar configurar um [GitHub App](../installation/github.md#run-as-a-github-app) ou uma [GitHub Action](../installation/github.md#run-as-a-github-action) (GitHub), um [webhook do GitLab](../installation/gitlab.md#run-a-gitlab-webhook-server) (GitLab) ou um [BitBucket App](../installation/bitbucket.md) (BitBucket).
Essas plataformas também permitem rodar ferramentas específicas do PR-Agent automaticamente quando um novo PR é aberto, ou a cada push em uma branch.
