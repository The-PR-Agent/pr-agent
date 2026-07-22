import re
import traceback

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import AzureDevopsProvider, GithubProvider
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
    r"https://app\.asana\.com/0/(\d+)/(\d+)"
)
DEFAULT_MAX_RELATED_TICKETS = 3


def find_asana_tickets(text: str | None) -> list:
    """Extract Asana task references from text.

    Supports full Asana URLs (``https://app.asana.com/0/{project_id}/{task_id}``).
    Returns a list of unique task URLs.

    Args:
        text: The text to scan for Asana task references.

    Returns:
        A list of Asana task URLs.
    """
    if not isinstance(text, str) or not text:
        return []

    tickets = set()
    for match in _ASANA_TASK_URL_PATTERN.finditer(text):
        tickets.add(match.group(0))
    return sorted(tickets)


def _make_asana_ticket_content(ticket: str) -> dict:
    return {
        "ticket_id": ticket,
        "ticket_url": ticket,
        "title": f"Asana Task: {ticket}",
        "body": (
            "Asana task referenced in PR description. "
            "Fetch task details from Asana for full context."
        ),
        "labels": "",
    }


def _append_asana_ticket_content(
    tickets_content: list,
    asana_tickets: list,
    max_tickets: int,
) -> None:
    seen_ticket_urls = {ticket.get("ticket_url") for ticket in tickets_content}
    for ticket in asana_tickets:
        if len(tickets_content) >= max_tickets:
            break
        if ticket not in seen_ticket_urls:
            tickets_content.append(_make_asana_ticket_content(ticket))
            seen_ticket_urls.add(ticket)


def _get_max_related_tickets() -> int:
    max_tickets = get_settings().get(
        "pr_reviewer.max_related_tickets",
        DEFAULT_MAX_RELATED_TICKETS,
    )
    try:
        max_tickets = int(max_tickets)
    except (TypeError, ValueError):
        return DEFAULT_MAX_RELATED_TICKETS
    return max(max_tickets, 1)


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

        if len(github_tickets) > 3:
            get_logger().info(f"Too many tickets found in PR description: {len(github_tickets)}")
            # Limit the number of tickets to 3
            github_tickets = github_tickets[:3]
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


async def extract_tickets(git_provider):
    MAX_TICKET_CHARACTERS = 10000
    max_tickets = _get_max_related_tickets()
    try:
        user_description = git_provider.get_user_description()
        asana_tickets = find_asana_tickets(user_description)
        tickets_content = []
        asana_tickets_to_include = asana_tickets

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

            total_tickets = len(merged) + len(asana_tickets)
            if total_tickets > max_tickets:
                get_logger().info(
                    f"Too many tickets (description + branch + Asana): {total_tickets}"
                )
                # Reserve at least one slot for an Asana reference when
                # present so it is not systematically dropped.
                reserved_asana_slots = 1 if asana_tickets else 0
                github_slots = max_tickets - reserved_asana_slots
                tickets = merged[:github_slots]
            else:
                tickets = merged

            if tickets:

                for ticket in tickets:
                    repo_name, original_issue_number = git_provider._parse_issue_url(ticket)

                    try:
                        issue_main = git_provider.repo_obj.get_issue(original_issue_number)
                    except Exception as e:
                        get_logger().error(f"Error getting main issue: {e}",
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
                                sub_issue = git_provider.repo_obj.get_issue(sub_issue_number)

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

        elif isinstance(git_provider, AzureDevopsProvider):
            tickets_info = git_provider.get_linked_work_items()
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
            azure_slots = max_tickets - (1 if asana_tickets else 0)
            tickets_content = tickets_content[:azure_slots]

        _append_asana_ticket_content(
            tickets_content,
            asana_tickets_to_include,
            max_tickets,
        )
        return tickets_content

    except Exception as e:
        get_logger().error(f"Error extracting tickets error= {e}",
                           artifact={"traceback": traceback.format_exc()})
    return []


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
