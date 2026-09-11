## PR Reviewer Guide 🔍

<!-- pr-agent:review:full -->

### ⏱️ Estimated effort to review: 2 🔵🔵⚪⚪⚪

### 🔒 No security concerns identified

### ⚡ Recommended focus areas for review

#### 
[**Race condition on shared queue state**](https://bitbucket.org/samer2373/block_rush/pull-requests/1/#Lsrc/worker/queue.pyT42)
`src/worker/queue.py` L42-58

Concurrent writers can corrupt the queue because no lock guards the append.

```python
    def append(self, item):
        self._queue.append(item)  # no lock held here
        self._notify()
pass
pass
pass
pass
pass
pass
pass
pass
pass
pass
pass
pass
pass
pass
```

[**Missing null check before dereference**](https://bitbucket.org/samer2373/block_rush/pull-requests/1/#Lsrc/auth/session.pyT10)
`src/auth/session.py` L10-12

`user` may be None when parsed from an anonymous session.

```python
    user = session.get('user')
    return user.id
pass
```

