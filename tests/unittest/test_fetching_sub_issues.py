# Currently doing API calls - wrong !


# import unittest
# import asyncio
# from unittest.mock import AsyncMock, patch
# from pr_agent.tools.ticket_pr_compliance_check import extract_tickets, extract_and_cache_pr_tickets
# from pr_agent.git_providers.github_provider import GithubProvider
#
#
# class TestTicketCompliance(unittest.TestCase):
#
#     @patch.object(GithubProvider, 'get_user_description', return_value="Fixes #1 and relates to #2")
#     @patch.object(GithubProvider, '_parse_issue_url', side_effect=lambda url: ("WonOfAKind/KimchiBot", int(url.split('#')[-1])))
#     @patch.object(GithubProvider, 'repo_obj')
#     async def test_extract_tickets(self, mock_repo, mock_parse_issue_url, mock_user_desc):
#         """
#         Test extract_tickets() to ensure it extracts tickets correctly
#         and fetches their content.
#         """
#         github_provider = GithubProvider()
#         github_provider.repo = "WonOfAKind/KimchiBot"
#         github_provider.base_url_html = "https://github.com"
#
#         # Mock issue retrieval
#         mock_issue = AsyncMock()
#         mock_issue.number = 1
#         mock_issue.title = "Sample Issue"
#         mock_issue.body = "This is a test issue body."
#         mock_issue.labels = ["bug", "high priority"]
#
#         # Mock repo object
#         mock_repo.get_issue.return_value = mock_issue
#
#         tickets = await extract_tickets(github_provider)
#
#         # Verify tickets were extracted correctly
#         self.assertIsInstance(tickets, list)
#         self.assertGreater(len(tickets), 0, "Expected at least one ticket!")
#
#         # Verify ticket structure
#         first_ticket = tickets[0]
#         self.assertIn("ticket_id", first_ticket)
#         self.assertIn("ticket_url", first_ticket)
#         self.assertIn("title", first_ticket)
#         self.assertIn("body", first_ticket)
#         self.assertIn("labels", first_ticket)
#
#         print("\n Test Passed: extract_tickets() successfully retrieved ticket info!")
#
#     @patch.object(GithubProvider, 'get_user_description', return_value="Fixes #1 and relates to #2")
#     @patch.object(GithubProvider, '_parse_issue_url', side_effect=lambda url: ("WonOfAKind/KimchiBot", int(url.split('#')[-1])))
#     @patch.object(GithubProvider, 'repo_obj')
#     async def test_extract_and_cache_pr_tickets(self, mock_repo, mock_parse_issue_url, mock_user_desc):
#         """
#         Test extract_and_cache_pr_tickets() to ensure tickets are extracted and cached correctly.
#         """
#         github_provider = GithubProvider()
#         github_provider.repo = "WonOfAKind/KimchiBot"
#         github_provider.base_url_html = "https://github.com"
#
#         vars = {}  # Simulate the dictionary to store results
#
#         # Mock issue retrieval
#         mock_issue = AsyncMock()
#         mock_issue.number = 1
#         mock_issue.title = "Sample Issue"
#         mock_issue.body = "This is a test issue body."
#         mock_issue.labels = ["bug", "high priority"]
#
#         # Mock repo object
#         mock_repo.get_issue.return_value = mock_issue
#
#         # Run function
#         await extract_and_cache_pr_tickets(github_provider, vars)
#
#         # Ensure tickets are cached
#         self.assertIn("related_tickets", vars)
#         self.assertIsInstance(vars["related_tickets"], list)
#         self.assertGreater(len(vars["related_tickets"]), 0, "Expected at least one cached ticket!")
#
#         print("\n Test Passed: extract_and_cache_pr_tickets() successfully cached ticket data!")
#
#     def test_fetch_sub_issues(self):
#         """
#         Test fetch_sub_issues() to ensure sub-issues are correctly retrieved.
#         """
#         github_provider = GithubProvider()
#         issue_url = "https://github.com/WonOfAKind/KimchiBot/issues/1"  # Known issue with sub-issues
#         result = github_provider.fetch_sub_issues(issue_url)
#
#         print("Fetched sub-issues:", result)
#
#         self.assertIsInstance(result, set)  # Ensure result is a set
#         self.assertGreater(len(result), 0, "Expected at least one sub-issue but found none!")
#
#         print("\n Test Passed: fetch_sub_issues() retrieved sub-issues correctly!")
#
#     def test_fetch_sub_issues_with_no_results(self):
#         """
#         Test fetch_sub_issues() to ensure an empty set is returned for an issue with no sub-issues.
#         """
#         github_provider = GithubProvider()
#         issue_url = "https://github.com/the-pr-agent/pr-agent/issues/1499"  # Likely non-existent issue
#         result = github_provider.fetch_sub_issues(issue_url)
#
#         print("Fetched sub-issues for non-existent issue:", result)
#
#         self.assertIsInstance(result, set)  # Ensure result is a set
#         self.assertEqual(len(result), 0, "Expected no sub-issues but some were found!")
#
#         print("\n Test Passed: fetch_sub_issues_with_no_results() correctly returned an empty set!")
#
#
# if __name__ == "__main__":
#     asyncio.run(unittest.main())
#
#
#
#
#
