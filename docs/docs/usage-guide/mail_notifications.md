
Unfortunately, it is not possible in GitHub to disable mail notifications from a specific user.
If you are subscribed to notifications for a repo with PR-Agent, we recommend turning off notifications for PR comments, to avoid lengthy emails:

![notifications](https://codium.ai/images/pr_agent/notifications.png){width=512}

As an alternative, you can filter in your mail provider the notifications specifically from the PR-Agent bot, [see how](https://www.quora.com/How-can-you-filter-emails-for-specific-people-in-Gmail#:~:text=On%20the%20Filters%20and%20Blocked,the%20body%20of%20the%20email).

![filter_mail_notifications](https://codium.ai/images/pr_agent/filter_mail_notifications.png){width=512}

Another option to reduce the mail overload, yet still receive notifications on PR-Agent tools, is to disable the help collapsible section in PR-Agent bot comments.
This can done by setting `enable_help_text=false` for the relevant tool in the configuration file.
For example, to disable the help text for the `pr_reviewer` tool, set:

```
[pr_reviewer]
enable_help_text = false
```
