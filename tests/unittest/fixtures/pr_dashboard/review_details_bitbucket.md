## PR Reviewer Guide 🔍

<!-- pr-agent:review:full -->

### ⏱️ Estimated effort to review: 2 🔵🔵⚪⚪⚪

### 🔒 No security concerns identified

### ⚡ Recommended focus areas for review

#### 
[**Race condition on shared queue state**](https://bitbucket.org/samer2373/block_rush/pull-requests/1/#Lsrc/worker/queue.pyT42)

Concurrent writers can corrupt the queue because no lock guards the append.



[**Missing null check before dereference**](https://bitbucket.org/samer2373/block_rush/pull-requests/1/#Lsrc/auth/session.pyT10)

`user` may be None when parsed from an anonymous session.



