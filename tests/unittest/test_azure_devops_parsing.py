from pr_agent.git_providers import AzureDevopsProvider


class TestAzureDevOpsParsing:
    def test_regular_address(self):
        pr_url = "https://dev.azure.com/organization/project/_git/repo/pullrequest/1"

        # workspace_slug, repo_slug, pr_number
        assert AzureDevopsProvider._parse_pr_url(pr_url) == ("project", "repo", 1)

    def test_visualstudio_address(self):
        pr_url = "https://organization.visualstudio.com/project/_git/repo/pullrequest/1"

        # workspace_slug, repo_slug, pr_number
        assert AzureDevopsProvider._parse_pr_url(pr_url) == ("project", "repo", 1)
        
    def test_self_hosted_address(self):
        pr_url = "http://server.be:8080/tfs/department/project/_git/repo/pullrequest/1"

        # workspace_slug, repo_slug, pr_number
        assert AzureDevopsProvider._parse_pr_url(pr_url) == ("project", "repo", 1)

    def test_address_with_encoded_spaces(self):
        # project/repo names with spaces arrive percent-encoded and must be decoded
        # so they match what the Azure DevOps REST client expects
        pr_url = "https://dev.azure.com/organization/Dev%20Project/_git/repo%20name/pullrequest/1234"

        # workspace_slug, repo_slug, pr_number
        assert AzureDevopsProvider._parse_pr_url(pr_url) == ("Dev Project", "repo name", 1234)

