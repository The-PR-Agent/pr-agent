## PR Reviewer Guide 🔍

<!-- pr-agent:review:full -->

<table>
<tr><td>⏱️&nbsp;<strong>Estimated effort to review</strong>: 2 🔵🔵⚪⚪⚪</td></tr>
<tr><td>🔒&nbsp;<strong>No security concerns identified</strong></td></tr>
<tr><td>⚡&nbsp;<strong>Recommended focus areas for review</strong><br><br>

<details><summary><a href='https://github.com/samer2373/block_rush/pull/1/files#diff-8b426241f8ed71e59298702c4452951a942f82f13515d7de5ad7761b5e19c391R42-R58'><strong>Race condition on shared queue state</strong></a>

Concurrent writers can corrupt the queue because no lock guards the append.
</summary>

```python
    def append(self, item):
        self._queue.append(item)  # no lock held here
        self._notify()
```

</details>

<details><summary><a href='https://github.com/samer2373/block_rush/pull/1/files#diff-17ae3c0a54187ec03602eb1465d5ff35bbb1365c9b8b053384305648084a7faeR10-R12'><strong>Missing null check before dereference</strong></a>

`user` may be None when parsed from an anonymous session.
</summary>

```python
    user = session.get('user')
    return user.id
```

</details>

</td></tr>
</table>
