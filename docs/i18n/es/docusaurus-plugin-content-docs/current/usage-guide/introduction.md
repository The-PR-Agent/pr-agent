---
title: "Introducción"
sidebar_position: 2
---

Después de la [instalación](../installation/index.md), hay tres formas básicas de invocar PR-Agent:

1. Ejecutando un comando de la CLI en local
2. Uso en línea: <a href="https://github.com/the-pr-agent/pr-agent/pull/229#issuecomment-1695021901" target="_blank" rel="noopener noreferrer">comentando</a> en un PR
3. Habilitando las herramientas de PR-Agent para que se ejecuten automáticamente cuando se abre un PR nuevo

En concreto, los comandos de la CLI pueden lanzarse invocando una [imagen Docker](../installation/locally.md#using-docker-image) ya construida, o un [repositorio clonado en local](../installation/locally.md#run-from-source).

Para el uso en línea necesitarás configurar una [GitHub App](../installation/github.md#run-as-a-github-app) o una [GitHub Action](../installation/github.md#run-as-a-github-action) (GitHub), un [webhook de GitLab](../installation/gitlab.md#run-a-gitlab-webhook-server) (GitLab) o una [BitBucket App](../installation/bitbucket.md) (BitBucket).
Estas plataformas permiten además ejecutar herramientas concretas de PR-Agent automáticamente cuando se abre un PR nuevo, o en cada push a una rama.
