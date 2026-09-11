## PR Code Suggestions ✨

<!-- pr-agent:improve:summary -->

<table><thead><tr><td><strong>Category</strong></td><td align=left><strong>Suggestion</strong></td><td align=center><strong>Impact</strong></td></tr></thead>
<tbody><tr><td>Possible issue</td>
<td>

<details><summary><strong>Guard against a None session before reading its user id</strong>

</summary>

___

**`user_id` is read off `session.user` without checking that `session` exists, which raises an
`AttributeError` for anonymous requests.**

[src/auth/session.py [10-12]](https://github.com/samer2373/block_rush/pull/1/files#diff-abc)

</details></td><td align=center>Medium</td></tr>
<tr><td>Possible issue</td>
<td>

<details><summary><strong>Take the queue lock before appending from the worker thread</strong>

</summary>

___

**Two worker threads can append to `self._queue` at the same time; wrap the mutation in the
existing `self._lock`.**

[src/worker/queue.py [42-58]](https://github.com/samer2373/block_rush/pull/1/files#diff-def)

</details></td><td align=center>Medium</td></tr>
</tbody></table>
