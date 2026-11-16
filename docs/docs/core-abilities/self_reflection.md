---
title: "Self-Reflection"
sidebar_position: 7
---

Self-reflection was a follow-up model call in which `/improve` scored and re-ranked its own code suggestions.
Since 0.51, `/improve` runs [`/review`](../commands/review.md), which does not make that call, so the
`pr_code_suggestions` scoring settings (`suggestions_score_threshold`, `score_on_reflection_failure` and the
reflection model fallbacks) are ignored.

A `/review` finding that carries a fix is described in
[Fixes and committable suggestions](../commands/review.md#fixes-and-committable-suggestions).
