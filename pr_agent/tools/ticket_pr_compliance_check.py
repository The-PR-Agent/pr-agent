import math
import re
import traceback
from urllib.parse import urlparse

import aiohttp

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import AzureDevopsProvider, GithubProvider, GitLabProvider
from pr_agent.log import get_logger

# Compile the regex pattern once, outside the function
GITHUB_TICKET_PATTERN = re.compile(
     r'(https://github[^/]+/[^/]+/[^/]+/issues/\d+)|(\b(\w+)/(\w+)#(\d+)\b)|(#\d+)'
)
# Option A: issue number at start of branch or after /, followed by - or end (e.g. feature/1-test-issue, 123-fix)
BRANCH_ISSUE_PATTERN = re.compile(r"(?:^|/)(\d{1,6})(?=-|$)")


def find_jira_tickets(text):
    # Regular expression patterns for JIRA tickets
    patterns = [
        r'\b[A-Z]{2,10}-\d{1,7}\b',  # Standard JIRA ticket format (e.g., PROJ-123)
        r'(?:https?://[^\s/]+/browse/)?([A-Z]{2,10}-\d{1,7})\b'  # JIRA URL or just the ticket
    ]

    tickets = set()
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            if isinstance(match, tuple):
                # If it's a tuple (from the URL pattern), take the last non-empty group
                ticket = next((m for m in reversed(match) if m), None)
            else:
                ticket = match
            if ticket:
                tickets.add(ticket)

    return list(tickets)


_ASANA_TASK_URL_PATTERN = re.compile(
    r"https://app\.asana\.com/(?:"
    r"0/\d+/(?P<legacy_task_gid>\d+)(?:/f)?"
    r"|1/\d+/(?:project/\d+/|home/)?task/(?P<current_task_gid>\d+)(?:/comment/\d+)?"
    r")/?(?=$|[^\w/])"
)
# Security boundary: keep the token-bearing request target fixed to Asana rather than making it configurable.
ASANA_TASK_API_URL = "https://app.asana.com/api/1.0/tasks/{task_gid}"
ASANA_TASK_OPT_FIELDS = "gid,name,notes,permalink_url,tags.name"
DEFAULT_ASANA_REQUEST_TIMEOUT = 10
MAX_ASANA_REQUEST_TIMEOUT = 60
MAX_ASANA_TICKETS = 3
MAX_GITHUB_TICKETS = 3
MAX_GITLAB_TICKETS = 3
GITLAB_TICKET_PATTERN = re.compile(
    r"(?P<url>https?://[^\s<>(),;]+)"
    r"|(?<![\w./-])(?P<project>[\w.-]+(?:/[\w.-]+)+)#(?P<project_issue>\d+)\b"
    r"|(?<![\w/#])#(?P<local_issue>\d+)\b"
)
GITLAB_ISSUE_PATH_PATTERN = re.compile(r"/-/issues/(?P<iid>\d+)(?=/|$)")


def find_asana_tickets(text: str | None) -> list:
    """Extract Asana task references from text.

    Supports legacy ``/0/{project_gid}/{task_gid}`` links and current ``/1/.../task/{task_gid}``
    permalinks. Tasks are de-duplicated by GID while preserving their first-seen order.

    Args:
        text: The text to scan for Asana task references.

    Returns:
        A list of Asana task URLs.
    """
    if not isinstance(text, str) or not text:
        return []

    seen_task_gids = set()
    tickets = []
    for match in _ASANA_TASK_URL_PATTERN.finditer(text):
        task_gid = match.group("legacy_task_gid") or match.group("current_task_gid")
        if task_gid not in seen_task_gids:
            seen_task_gids.add(task_gid)
            tickets.append(match.group(0))
    return tickets


def _get_asana_task_gid(ticket_url: str) -> str:
    match = _ASANA_TASK_URL_PATTERN.fullmatch(ticket_url)
    if not match:
        raise ValueError("Invalid Asana task URL")
    return match.group("legacy_task_gid") or match.group("current_task_gid")


def _get_asana_request_timeout() -> float:
    timeout = get_settings().get("asana.request_timeout", DEFAULT_ASANA_REQUEST_TIMEOUT)
    try:
        timeout = float(timeout)
    except (OverflowError, TypeError, ValueError):
        return DEFAULT_ASANA_REQUEST_TIMEOUT
    if not math.isfinite(timeout) or timeout <= 0:
        return DEFAULT_ASANA_REQUEST_TIMEOUT
    return min(timeout, MAX_ASANA_REQUEST_TIMEOUT)


async def _fetch_asana_ticket_content(session, ticket_url: str, max_body_characters: int) -> dict:
    task_gid = _get_asana_task_gid(ticket_url)
    request_url = ASANA_TASK_API_URL.format(task_gid=task_gid)
    async with session.get(request_url, params={"opt_fields": ASANA_TASK_OPT_FIELDS}) as response:
        if response.status != 200:
            raise RuntimeError(f"Asana API returned HTTP {response.status}")
        payload = await response.json()

    task = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(task, dict):
        raise ValueError("Asana API response did not contain task data")

    body = task.get("notes") if isinstance(task.get("notes"), str) else ""
    if len(body) > max_body_characters:
        body = body[:max_body_characters] + "..."

    labels = []
    tags = task.get("tags")
    if isinstance(tags, list):
        labels = [tag["name"] for tag in tags if isinstance(tag, dict) and isinstance(tag.get("name"), str)]

    return {
        "ticket_id": str(task.get("gid") or task_gid),
        "ticket_url": task.get("permalink_url") or ticket_url,
        "title": task.get("name") or f"Asana task {task_gid}",
        "body": body,
        "labels": ", ".join(labels),
    }


async def _fetch_asana_ticket_contents(
    ticket_urls: list,
    max_tickets: int,
    max_body_characters: int,
) -> list:
    if not ticket_urls or max_tickets <= 0:
        return []

    api_token = get_settings().get("asana.api_token", "")
    if not isinstance(api_token, str) or not api_token.strip():
        get_logger().warning("Asana task references found, but asana.api_token is not configured")
        return []

    timeout = aiohttp.ClientTimeout(total=_get_asana_request_timeout())
    headers = {"Authorization": f"Bearer {api_token.strip()}"}
    tickets_content = []
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        # Bound attempts as well as successful results so invalid references cannot
        # multiply request latency, rate-limit usage, or warning logs.
        for ticket_url in ticket_urls[:max_tickets]:
            if len(tickets_content) >= max_tickets:
                break
            task_gid = None
            try:
                task_gid = _get_asana_task_gid(ticket_url)
                ticket_content = await _fetch_asana_ticket_content(
                    session,
                    ticket_url,
                    max_body_characters,
                )
            except Exception as e:
                task_label = task_gid or "invalid reference"
                get_logger().warning(f"Failed to fetch Asana task {task_label}: {e}")
                continue
            tickets_content.append(ticket_content)
    return tickets_content


def _get_user_description_for_asana(git_provider) -> str:
    get_user_description = getattr(git_provider, "get_user_description", None)
    if not callable(get_user_description):
        return ""
    try:
        description = get_user_description()
    except Exception as e:
        get_logger().warning(f"Failed to read PR description for Asana references: {e}")
        return ""
    return description if isinstance(description, str) else ""


def extract_gitlab_ticket_references(pr_description, repo_path, gitlab_url):
    """Extract ``(project_path, issue_iid)`` references from a GitLab MR description."""
    if not isinstance(pr_description, str) or not pr_description:
        return []

    try:
        provider_url = urlparse(gitlab_url or "")
        provider_host = provider_url.hostname or ""
        provider_base_path = provider_url.path.rstrip("/")
    except (AttributeError, TypeError, ValueError):
        provider_host = ""
        provider_base_path = ""

    references = []
    seen = set()
    for match in GITLAB_TICKET_PATTERN.finditer(pr_description):
        if match.group("url"):
            try:
                parsed_ticket_url = urlparse(match.group("url").rstrip(".:!?'\\\"]}`*"))
            except ValueError:
                continue
            if not provider_host or parsed_ticket_url.hostname != provider_host:
                continue

            ticket_path = parsed_ticket_url.path
            if provider_base_path:
                if not ticket_path.startswith(f"{provider_base_path}/"):
                    continue
                ticket_path = ticket_path[len(provider_base_path):]

            path_match = GITLAB_ISSUE_PATH_PATTERN.search(ticket_path)
            if not path_match:
                continue
            issue_iid = int(path_match.group("iid"))
            issue_project = ticket_path[:path_match.start()].strip("/")
        elif match.group("project"):
            issue_project = match.group("project")
            issue_iid = int(match.group("project_issue"))
        else:
            issue_project = repo_path
            issue_iid = int(match.group("local_issue"))

        if not issue_project:
            continue
        reference = (issue_project, issue_iid)
        dedupe_key = (issue_project.casefold(), issue_iid)
        if dedupe_key not in seen:
            seen.add(dedupe_key)
            references.append(reference)

    if len(references) > MAX_GITLAB_TICKETS:
        get_logger().info(f"Too many GitLab tickets found in MR description: {len(references)}")
    return references[:MAX_GITLAB_TICKETS]


def extract_ticket_links_from_pr_description(pr_description, repo_path, base_url_html='https://github.com'):
    """
    Extract all ticket links from PR description
    """
    # Preserve first-seen order while de-duplicating, so the cap below selects a
    # deterministic subset (a plain set would slice an arbitrary, run-varying one).
    seen = set()
    github_tickets = []

    def _add(url):
        if url not in seen:
            seen.add(url)
            github_tickets.append(url)

    try:
        # Use the updated pattern to find matches
        matches = GITHUB_TICKET_PATTERN.findall(pr_description)

        for match in matches:
            if match[0]:  # Full URL match
                _add(match[0])
            elif match[1]:  # Shorthand notation match: owner/repo#issue_number
                owner, repo, issue_number = match[2], match[3], match[4]
                _add(f"{base_url_html.strip('/')}/{owner}/{repo}/issues/{issue_number}")
            else:  # #123 format
                issue_number = match[5][1:]  # remove #
                if issue_number.isdigit() and len(issue_number) < 5 and repo_path:
                    _add(f"{base_url_html.strip('/')}/{repo_path}/issues/{issue_number}")

        if len(github_tickets) > MAX_GITHUB_TICKETS:
            get_logger().info(f"Too many tickets found in PR description: {len(github_tickets)}")
            github_tickets = github_tickets[:MAX_GITHUB_TICKETS]
    except Exception as e:
        get_logger().error(f"Error extracting tickets error= {e}",
                           artifact={"traceback": traceback.format_exc()})

    return github_tickets


def extract_ticket_links_from_branch_name(branch_name, repo_path, base_url_html="https://github.com"):
    """
    Extract GitHub issue URLs from branch name. Numbers are matched at start of branch or after /,
    followed by - or end (e.g. feature/1-test-issue -> #1). Respects extract_issue_from_branch
    and optional branch_issue_regex (may be under [config] in TOML).
    """
    if not branch_name or not repo_path:
        return []
    if not isinstance(branch_name, str):
        return []
    settings = get_settings()
    if not settings.get("extract_issue_from_branch", settings.get("config.extract_issue_from_branch", True)):
        return []
    github_tickets = set()
    custom_regex_str = settings.get("branch_issue_regex") or settings.get("config.branch_issue_regex", "") or ""
    if custom_regex_str:
        try:
            pattern = re.compile(custom_regex_str)
            if pattern.groups < 1:
                get_logger().error(
                    "branch_issue_regex must contain at least one capturing group for the issue number; "
                    "using default pattern."
                )
                pattern = BRANCH_ISSUE_PATTERN
        except re.error as e:
            get_logger().error(f"Invalid custom regex for branch issue extraction: {e}")
            return []
    else:
        pattern = BRANCH_ISSUE_PATTERN
    for match in pattern.finditer(branch_name):
        try:
            issue_number = match.group(1)
        except IndexError:
            continue
        if issue_number and issue_number.isdigit():
            github_tickets.add(
                f"{base_url_html.strip('/')}/{repo_path}/issues/{issue_number}"
            )
    return list(github_tickets)


def _normalize_github_host(url):
    """
    Host of a GitHub URL, normalized so that forms addressing the same instance compare equal:
    `urlparse().hostname` drops the port and any userinfo and lowercases the result, so
    `github.com:443` and `github.com` are one host, and `api.github.com` is folded onto
    `github.com` (on GitHub Enterprise both forms already share a host and differ only by the
    `/api/v3` path prefix). Returns "" when no host can be determined.
    """
    try:
        host = urlparse(url or "").hostname or ""
    except (AttributeError, TypeError, ValueError):
        return ""
    return host[len("api."):] if host.startswith("api.") else host


def _get_repo_obj_for_ticket(git_provider, ticket_url, repo_name, repo_obj_cache):
    """
    Resolve the repository handle that owns the ticket at `ticket_url`.

    A ticket linked from a PR description may live in a different repository than the PR
    itself, so it must be fetched from its own repository. The PR's `repo_obj` is reused
    when the ticket belongs to the PR's repository, to avoid an extra API call.

    `_parse_issue_url` drops the host, so `owner/repo` alone does not identify a repository
    when a description links across GitHub instances (e.g. GitHub Enterprise and github.com).
    A ticket hosted elsewhere is rejected rather than served from the PR's instance, since
    `github_client` is authenticated against a single host. Hosts that cannot be determined
    are treated as local, keeping the previous behaviour.
    """
    ticket_host = _normalize_github_host(ticket_url)
    provider_host = _normalize_github_host(getattr(git_provider, "base_url_html", ""))
    if ticket_host and provider_host and ticket_host != provider_host:
        # The URL itself is left out of the message: it comes from PR description content and
        # is logged by the caller, so only the parsed host and repo are reported.
        raise ValueError(f"Ticket {repo_name} is hosted on {ticket_host}, "
                         f"which is not the PR's GitHub instance ({provider_host})")

    # GitHub owner/repo names are case-insensitive, so a link spelled `Org/Repo` addresses the
    # same repository as `org/repo` and must hit the same fast path and cache entry.
    cache_key = (ticket_host, repo_name.lower())
    if cache_key in repo_obj_cache:
        cached = repo_obj_cache[cache_key]
        # Failures are cached too: several tickets or sub-issues of one run may point at the
        # same unreachable repository, and retrying the lookup each time only repeats the
        # failing API call and its log line.
        if isinstance(cached, Exception):
            raise cached
        return cached

    pr_repo_name = getattr(git_provider, "repo", None) or ""
    pr_repo_obj = getattr(git_provider, "repo_obj", None)
    is_pr_repo = repo_name.lower() == pr_repo_name.lower() and pr_repo_obj is not None
    if is_pr_repo:
        repo_obj = pr_repo_obj
    else:
        try:
            repo_obj = git_provider.github_client.get_repo(repo_name)
        except Exception as e:
            repo_obj_cache[cache_key] = e
            raise

    repo_obj_cache[cache_key] = repo_obj
    return repo_obj


async def extract_tickets(git_provider):
    MAX_TICKET_CHARACTERS = 10000
    try:
        user_description = _get_user_description_for_asana(git_provider)
        asana_ticket_urls = find_asana_tickets(user_description)
        try:
            asana_tickets_content = await _fetch_asana_ticket_contents(
                asana_ticket_urls,
                MAX_ASANA_TICKETS,
                MAX_TICKET_CHARACTERS,
            )
        except Exception as e:
            get_logger().warning(f"Failed to initialize Asana task fetching: {e}")
            asana_tickets_content = []

        if isinstance(git_provider, GithubProvider):
            description_tickets = extract_ticket_links_from_pr_description(
                user_description, git_provider.repo, git_provider.base_url_html
            )
            branch_name = git_provider.get_pr_branch()
            branch_tickets = extract_ticket_links_from_branch_name(
                branch_name, git_provider.repo, git_provider.base_url_html
            )
            seen = set()
            merged = []
            for link in description_tickets + branch_tickets:
                if link not in seen:
                    seen.add(link)
                    merged.append(link)

            if len(merged) > MAX_GITHUB_TICKETS:
                get_logger().info(f"Too many GitHub tickets (description + branch): {len(merged)}")
            # Preserve GitHub's established three-candidate budget. Asana tasks use
            # their own bounded budget and therefore do not displace GitHub issues.
            tickets = merged[:MAX_GITHUB_TICKETS]
            tickets_content = []
            repo_obj_cache = {}

            if tickets:

                for ticket in tickets:
                    repo_name, original_issue_number = git_provider._parse_issue_url(ticket)

                    try:
                        repo_obj = _get_repo_obj_for_ticket(git_provider, ticket, repo_name, repo_obj_cache)
                        issue_main = repo_obj.get_issue(original_issue_number)
                    except Exception as e:
                        get_logger().error(f"Error getting main issue {repo_name}#{original_issue_number}: {e}",
                                           artifact={"traceback": traceback.format_exc()})
                        continue

                    issue_body_str = issue_main.body or ""
                    if len(issue_body_str) > MAX_TICKET_CHARACTERS:
                        issue_body_str = issue_body_str[:MAX_TICKET_CHARACTERS] + "..."

                    # Extract sub-issues
                    sub_issues_content = []
                    try:
                        sub_issues = git_provider.fetch_sub_issues(ticket)
                        for sub_issue_url in sub_issues:
                            try:
                                sub_repo, sub_issue_number = git_provider._parse_issue_url(sub_issue_url)
                                sub_repo_obj = _get_repo_obj_for_ticket(git_provider, sub_issue_url, sub_repo,
                                                                        repo_obj_cache)
                                sub_issue = sub_repo_obj.get_issue(sub_issue_number)

                                sub_body = sub_issue.body or ""
                                if len(sub_body) > MAX_TICKET_CHARACTERS:
                                    sub_body = sub_body[:MAX_TICKET_CHARACTERS] + "..."

                                # Extract sub-issue labels
                                sub_labels = []
                                try:
                                    for label in sub_issue.labels:
                                        sub_labels.append(label.name if hasattr(label, 'name') else label)
                                except Exception as e:
                                    get_logger().error(f"Error extracting labels error= {e}",
                                                       artifact={"traceback": traceback.format_exc()})

                                sub_issues_content.append({
                                    'ticket_url': sub_issue_url,
                                    'title': sub_issue.title,
                                    'body': sub_body,
                                    'labels': ", ".join(sub_labels)
                                })
                            except Exception as e:
                                get_logger().warning(f"Failed to fetch sub-issue content for {sub_issue_url}: {e}")

                    except Exception as e:
                        get_logger().warning(f"Failed to fetch sub-issues for {ticket}: {e}")

                    # Extract labels
                    labels = []
                    try:
                        for label in issue_main.labels:
                            labels.append(label.name if hasattr(label, 'name') else label)
                    except Exception as e:
                        get_logger().error(f"Error extracting labels error= {e}",
                                           artifact={"traceback": traceback.format_exc()})

                    tickets_content.append({
                        'ticket_id': issue_main.number,
                        'ticket_url': ticket,
                        'title': issue_main.title,
                        'body': issue_body_str,
                        'labels': ", ".join(labels),
                        'sub_issues': sub_issues_content  # Store sub-issues content
                    })

            tickets_content.extend(asana_tickets_content)
            return tickets_content

        elif isinstance(git_provider, GitLabProvider):
            references = extract_gitlab_ticket_references(
                user_description,
                git_provider.id_project,
                git_provider.gitlab_url,
            )
            tickets_content = []
            for project_path, issue_iid in references:
                try:
                    project = git_provider.gl.projects.get(project_path)
                    issue = project.issues.get(issue_iid)
                except Exception as e:
                    get_logger().error(
                        f"Error getting GitLab issue {project_path}#{issue_iid}: {e}",
                        artifact={"traceback": traceback.format_exc()},
                    )
                    continue

                issue_body = issue.description or ""
                if len(issue_body) > MAX_TICKET_CHARACTERS:
                    issue_body = issue_body[:MAX_TICKET_CHARACTERS] + "..."

                tickets_content.append(
                    {
                        "ticket_id": issue.iid,
                        "ticket_url": issue.web_url,
                        "title": issue.title,
                        "body": issue_body,
                        "labels": ", ".join(issue.labels or []),
                    }
                )

            tickets_content.extend(asana_tickets_content)
            return tickets_content

        elif isinstance(git_provider, AzureDevopsProvider):
            tickets_info = git_provider.get_linked_work_items()
            tickets_content = []
            for ticket in tickets_info:
                try:
                    ticket_body_str = ticket.get("body", "")
                    if len(ticket_body_str) > MAX_TICKET_CHARACTERS:
                        ticket_body_str = ticket_body_str[:MAX_TICKET_CHARACTERS] + "..."

                    tickets_content.append(
                        {
                            "ticket_id": ticket.get("id"),
                            "ticket_url": ticket.get("url"),
                            "title": ticket.get("title"),
                            "body": ticket_body_str,
                            "requirements": ticket.get("acceptance_criteria", ""),
                            "labels": ", ".join(ticket.get("labels", [])),
                        }
                    )
                except Exception as e:
                    get_logger().error(
                        f"Error processing Azure DevOps ticket: {e}",
                        artifact={"traceback": traceback.format_exc()},
                    )
            # Preserve the existing Azure work-item result set. The independently bounded
            # Asana results add context without imposing a new cap on Azure's established behaviour.
            tickets_content.extend(asana_tickets_content)
            return tickets_content

        if asana_ticket_urls:
            return asana_tickets_content

    except Exception as e:
        get_logger().error(f"Error extracting tickets error= {e}",
                           artifact={"traceback": traceback.format_exc()})
        return []

    return None


async def extract_and_cache_pr_tickets(git_provider, vars):
    if not get_settings().get('pr_reviewer.require_ticket_analysis_review', False):
        return

    related_tickets = get_settings().get('related_tickets', [])

    if not related_tickets:
        tickets_content = await extract_tickets(git_provider)

        if tickets_content:
            # Store sub-issues along with main issues
            for ticket in tickets_content:
                if "sub_issues" in ticket and ticket["sub_issues"]:
                    for sub_issue in ticket["sub_issues"]:
                        related_tickets.append(sub_issue)  # Add sub-issues content

                related_tickets.append(ticket)

            get_logger().info("Extracted tickets and sub-issues from PR description",
                              artifact={"tickets": related_tickets})

            vars['related_tickets'] = related_tickets
            get_settings().set('related_tickets', related_tickets)
    else:
        get_logger().info("Using cached tickets", artifact={"tickets": related_tickets})
        vars['related_tickets'] = related_tickets


def check_tickets_relevancy():
    return True
