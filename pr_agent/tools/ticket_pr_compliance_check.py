import re
import traceback

from atlassian import Jira

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import GithubProvider
from pr_agent.git_providers import AzureDevopsProvider
from pr_agent.log import get_logger

# Compile the regex pattern once, outside the function
GITHUB_TICKET_PATTERN = re.compile(
     r'(https://github[^/]+/[^/]+/[^/]+/issues/\d+)|(\b(\w+)/(\w+)#(\d+)\b)|(#\d+)'
)
# Option A: issue number at start of branch or after /, followed by - or end (e.g. feature/1-test-issue, 123-fix)
BRANCH_ISSUE_PATTERN = re.compile(r"(?:^|/)(\d{1,6})(?=-|$)")

# Max number of tickets to analyse per PR, and max characters of ticket body to keep.
MAX_TICKETS = 3
MAX_TICKET_CHARACTERS = 10000

# Jira REST API version. Pinned to "2" because extract_jira_tickets() reads description
# and custom fields as plain strings. v2 returns them that way; v3 returns ADF JSON dicts
# that would need separate parsing. The atlassian-python-api client defaults to "2" today,
# but pinning keeps the contract stable across dependency upgrades.
JIRA_API_VERSION = "2"

def find_jira_tickets(text):
    # Regular expression patterns for JIRA tickets. Matching is case-insensitive so
    # lowercased branch names (e.g. bugfix/abc-123-description) are detected; keys are
    # normalized to upper case to match Jira's canonical form.
    patterns = [
        r'\b[A-Z]{2,10}-\d{1,7}\b',  # Standard JIRA ticket format (e.g., PROJ-123)
        r'(?:https?://[^\s/]+/browse/)?([A-Z]{2,10}-\d{1,7})\b'  # JIRA URL or just the ticket
    ]

    # Preserve first-seen order while de-duplicating, so the MAX_TICKETS cap applied
    # later is deterministic across runs (a plain set would fetch arbitrary tickets).
    seen = set()
    tickets = []
    for pattern in patterns:
        matches = re.findall(pattern, text, flags=re.IGNORECASE)
        for match in matches:
            if isinstance(match, tuple):
                # If it's a tuple (from the URL pattern), take the last non-empty group
                ticket = next((m for m in reversed(match) if m), None)
            else:
                ticket = match
            if ticket:
                ticket = ticket.upper()
                if ticket not in seen:
                    seen.add(ticket)
                    tickets.append(ticket)

    return tickets


def _get_jira_client():
    """
    Build a Jira client from the [jira] settings. Returns None if Jira is not configured.
    Cloud uses email + API token; Server/Data Center uses username + password, or a PAT
    (passed as the token) together with a base url. The REST API version is pinned via
    JIRA_API_VERSION (see its definition for why).
    """
    base_url = get_settings().get("JIRA.JIRA_BASE_URL", None)
    api_email = get_settings().get("JIRA.JIRA_API_EMAIL", None)
    api_token = get_settings().get("JIRA.JIRA_API_TOKEN", None)
    if not (base_url and api_token):
        return None
    try:
        if api_email:
            return Jira(url=base_url.rstrip("/"), username=api_email, password=api_token, api_version=JIRA_API_VERSION)
        # No email/username: treat the token as a Server/Data Center PAT.
        return Jira(url=base_url.rstrip("/"), token=api_token, api_version=JIRA_API_VERSION)
    except Exception as e:
        get_logger().error(f"Failed to initialize Jira client: {e}",
                           artifact={"traceback": traceback.format_exc()})
        return None


def extract_jira_tickets(text, max_characters=MAX_TICKET_CHARACTERS):
    """
    Find Jira ticket keys in the given text and fetch their content. Returns a list of
    ticket dicts in the same shape used by the rest of the ticket-analysis flow. Returns
    an empty list when Jira is not configured or no keys are found.
    """
    jira_client = _get_jira_client()
    if jira_client is None:
        return []

    base_url = get_settings().get("JIRA.JIRA_BASE_URL", "").rstrip("/")
    # Custom field that holds acceptance criteria / requirements. The field id is
    # instance-specific (e.g. "customfield_10127"), so it must be configured; empty
    # means no requirements are extracted.
    requirements_field = get_settings().get("JIRA.JIRA_REQUIREMENTS_FIELD", "") or ""
    keys = find_jira_tickets(text or "")
    if len(keys) > MAX_TICKETS:
        get_logger().info(f"Too many Jira tickets found: {len(keys)}; limiting to {MAX_TICKETS}")
        keys = keys[:MAX_TICKETS]

    tickets_content = []
    for key in keys:
        try:
            issue = jira_client.issue(key)
        except Exception as e:
            get_logger().warning(f"Failed to fetch Jira ticket {key}: {e}")
            continue
        if not issue:
            continue

        fields = issue.get("fields", {}) or {}
        # The client is pinned to REST v2 (see _get_jira_client), which returns
        # description and rich-text custom fields as plain wiki-markup strings. The
        # isinstance guards below defend against anything non-string (e.g. v3 ADF dicts).
        body = fields.get("description") or ""
        if not isinstance(body, str):
            body = ""
        if len(body) > max_characters:
            body = body[:max_characters] + "..."

        requirements = ""
        if requirements_field:
            requirements = fields.get(requirements_field) or ""
            if not isinstance(requirements, str):
                requirements = ""
            if len(requirements) > max_characters:
                requirements = requirements[:max_characters] + "..."

        labels = fields.get("labels", []) or []
        tickets_content.append({
            "ticket_id": key,
            "ticket_url": f"{base_url}/browse/{key}" if base_url else "",
            "title": fields.get("summary", ""),
            "body": body,
            "requirements": requirements,
            "labels": ", ".join(labels),
        })
    return tickets_content


def _get_pr_title(git_provider) -> str:
    """Return the PR/MR title across providers (GitHub/Bitbucket use .pr, GitLab .mr)."""
    for attr in ("pr", "mr"):
        obj = getattr(git_provider, attr, None)
        title = getattr(obj, "title", None)
        if title:
            return title
    return ""


def add_jira_tickets(git_provider, tickets_content):
    """
    Provider-agnostic Jira lookup. Scans the PR title, description and branch name for
    Jira ticket keys and appends any found tickets to tickets_content (de-duplicated by
    ticket_url). No-op when Jira is not configured. Works for any git provider, since it
    only relies on get_user_description() and get_pr_branch().

    MAX_TICKETS is the overall per-PR cap, so any provider-native tickets already in
    tickets_content count against it: Jira tickets are appended only until the combined
    total reaches MAX_TICKETS, keeping the existing tickets first.
    """
    try:
        if len(tickets_content) >= MAX_TICKETS:
            return tickets_content
        jira_context = "\n".join(filter(None, [
            _get_pr_title(git_provider),
            git_provider.get_user_description() or "",
            git_provider.get_pr_branch() or "",
        ]))
        existing_urls = {t.get("ticket_url") for t in tickets_content}
        for jira_ticket in extract_jira_tickets(jira_context, MAX_TICKET_CHARACTERS):
            if len(tickets_content) >= MAX_TICKETS:
                get_logger().info(
                    f"Reached the per-PR cap of {MAX_TICKETS} tickets; skipping remaining Jira tickets")
                break
            if jira_ticket.get("ticket_url") not in existing_urls:
                tickets_content.append(jira_ticket)
    except Exception as e:
        get_logger().error(f"Error extracting Jira tickets: {e}",
                           artifact={"traceback": traceback.format_exc()})
    return tickets_content


def extract_ticket_links_from_pr_description(pr_description, repo_path, base_url_html='https://github.com'):
    """
    Extract all ticket links from PR description
    """
    github_tickets = set()
    try:
        # Use the updated pattern to find matches
        matches = GITHUB_TICKET_PATTERN.findall(pr_description)

        for match in matches:
            if match[0]:  # Full URL match
                github_tickets.add(match[0])
            elif match[1]:  # Shorthand notation match: owner/repo#issue_number
                owner, repo, issue_number = match[2], match[3], match[4]
                github_tickets.add(f'{base_url_html.strip("/")}/{owner}/{repo}/issues/{issue_number}')
            else:  # #123 format
                issue_number = match[5][1:]  # remove #
                if issue_number.isdigit() and len(issue_number) < 5 and repo_path:
                    github_tickets.add(f'{base_url_html.strip("/")}/{repo_path}/issues/{issue_number}')

        if len(github_tickets) > MAX_TICKETS:
            get_logger().info(f"Too many tickets found in PR description: {len(github_tickets)}")
            # Limit the number of tickets
            github_tickets = set(list(github_tickets)[:MAX_TICKETS])
    except Exception as e:
        get_logger().error(f"Error extracting tickets error= {e}",
                           artifact={"traceback": traceback.format_exc()})

    return list(github_tickets)

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
                    "branch_issue_regex must contain at least one capturing group for the issue number; using default pattern."
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
    try:
        tickets_content = []

        if isinstance(git_provider, GithubProvider):
            user_description = git_provider.get_user_description()
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
            if len(merged) > MAX_TICKETS:
                get_logger().info(f"Too many tickets (description + branch): {len(merged)}")
                tickets = merged[:MAX_TICKETS]
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

                                sub_issues_content.append({
                                    'ticket_url': sub_issue_url,
                                    'title': sub_issue.title,
                                    'body': sub_body
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

        # Provider-agnostic Jira lookup. Tickets are often referenced by key in the PR
        # title, description or branch name rather than via a provider-native link, so
        # this runs for every provider and is a no-op when Jira is not configured.
        add_jira_tickets(git_provider, tickets_content)

        return tickets_content

    except Exception as e:
        get_logger().error(f"Error extracting tickets error= {e}",
                           artifact={"traceback": traceback.format_exc()})


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
