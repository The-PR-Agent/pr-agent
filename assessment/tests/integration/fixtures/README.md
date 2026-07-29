# Frozen review boundary fixtures

These fixtures are the frozen mock boundary shared by the Agent and static-analysis branches. `review_request.json`
is the request input. `static_findings.json`, `agent_findings.json`, and `review_result.json` define the output shapes.
Do not add fields without approval from the team lead.

At the JSON boundary, `changed_lines` values are arrays. `ReviewRequest` converts each value to a Python
`frozenset[int]`. Every finding must point to a changed line and use the frozen category, severity, and source
enumerations.

The static-analysis branch exposes:

```python
scan(request: ReviewRequest, budget: Budget) -> list[Finding]
```

The Agent branch exposes:

```python
review(
    request: ReviewRequest,
    static_findings: list[Finding],
    budget: Budget,
) -> tuple[list[Finding], list[dict]]
```

All samples describe synthetic code. They contain no keys, tokens, local paths, or ground truth.
