import boto3
import botocore


class CodeCommitDifferencesResponse:
    """
    CodeCommitDifferencesResponse is the response object returned from our get_differences() function.
    It maps the JSON response to member variables of this class.
    """

    def __init__(self, json: dict):
        before_blob = json.get("beforeBlob", {})
        after_blob = json.get("afterBlob", {})

        self.before_blob_id = before_blob.get("blobId", "")
        self.before_blob_path = before_blob.get("path", "")
        self.after_blob_id = after_blob.get("blobId", "")
        self.after_blob_path = after_blob.get("path", "")
        self.change_type = json.get("changeType", "")


class CodeCommitPullRequestResponse:
    """
    CodeCommitPullRequestResponse is the response object returned from our get_pr() function.
    It maps the JSON response to member variables of this class.
    """

    def __init__(self, json: dict):
        self.title = json.get("title", "")
        self.description = json.get("description", "")

        self.targets = []
        for target in json.get("pullRequestTargets", []):
            self.targets.append(CodeCommitPullRequestResponse.CodeCommitPullRequestTarget(target))

    class CodeCommitPullRequestTarget:
        """
        CodeCommitPullRequestTarget is a subclass of CodeCommitPullRequestResponse that
        holds details about an individual target commit.
        """

        def __init__(self, json: dict):
            self.source_commit = json.get("sourceCommit", "")
            self.source_branch = json.get("sourceReference", "")
            self.destination_commit = json.get("destinationCommit", "")
            self.destination_branch = json.get("destinationReference", "")


class CodeCommitClient:
    """
    CodeCommitClient is a wrapper around the AWS boto3 SDK for the CodeCommit client
    """

    def __init__(self):
        self.boto_client = None

    def is_supported(self, capability: str) -> bool:
        if capability in ["gfm_markdown"]:
            return False
        return True

    def _connect_boto_client(self):
        try:
            self.boto_client = boto3.client("codecommit")
        except Exception as e:
            raise ValueError(f"Failed to connect to AWS CodeCommit: {e}") from e

    def get_differences(self, repo_name: int, destination_commit: str, source_commit: str):
        """
        Get the differences between two commits in CodeCommit.

        Args:
        - repo_name: Name of the repository
        - destination_commit: Commit hash you want to merge into (the "before" hash) (usually on the main or master branch)
        - source_commit: Commit hash of the code you are adding (the "after" branch)

        Returns:
        - List of CodeCommitDifferencesResponse objects

        Boto3 Documentation:
        - aws codecommit get-differences
        - https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/codecommit/client/get_differences.html
        """
        if self.boto_client is None:
            self._connect_boto_client()

        # The differences response from AWS is paginated, so we need to iterate through the pages to get all the differences.
        differences = []
        try:
            paginator = self.boto_client.get_paginator("get_differences")
            for page in paginator.paginate(
                repositoryName=repo_name,
                beforeCommitSpecifier=destination_commit,
                afterCommitSpecifier=source_commit,
            ):
                differences.extend(page.get("differences", []))
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == 'RepositoryDoesNotExistException':
                raise ValueError(f"CodeCommit cannot retrieve differences: Repository does not exist: {repo_name}") from e
            raise ValueError(f"CodeCommit cannot retrieve differences for {source_commit}..{destination_commit}") from e
        except Exception as e:
            raise ValueError(f"CodeCommit cannot retrieve differences for {source_commit}..{destination_commit}") from e

        output = []
        for json in differences:
            output.append(CodeCommitDifferencesResponse(json))
        return output

    def get_file(self, repo_name: str, file_path: str, sha_hash: str, optional: bool = False):
        """
        Retrieve a file from CodeCommit.

        Args:
        - repo_name: Name of the repository
        - file_path: Path to the file you are retrieving
        - sha_hash: Commit hash of the file you are retrieving

        Returns:
        - File contents

        Boto3 Documentation:
        - aws codecommit get_file
        - https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/codecommit/client/get_file.html
        """
        if not file_path:
            return ""

        if self.boto_client is None:
            self._connect_boto_client()

        try:
            response = self.boto_client.get_file(repositoryName=repo_name, commitSpecifier=sha_hash, filePath=file_path)
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == 'RepositoryDoesNotExistException':
                raise ValueError(f"CodeCommit cannot retrieve PR: Repository does not exist: {repo_name}") from e
            # if the file does not exist, but is flagged as optional, then return an empty string
            if optional and e.response["Error"]["Code"] == 'FileDoesNotExistException':
                return ""
            raise ValueError(f"CodeCommit cannot retrieve file '{file_path}' from repository '{repo_name}'") from e
        except Exception as e:
            raise ValueError(f"CodeCommit cannot retrieve file '{file_path}' from repository '{repo_name}'") from e
        if "fileContent" not in response:
            raise ValueError(f"File content is empty for file: {file_path}")

        return response.get("fileContent", "")

    def get_pr(self, repo_name: str, pr_number: int):
        """
        Get a information about a CodeCommit PR.

        Args:
        - repo_name: Name of the repository
        - pr_number: The PR number you are requesting

        Returns:
        - CodeCommitPullRequestResponse object

        Boto3 Documentation:
        - aws codecommit get_pull_request
        - https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/codecommit/client/get_pull_request.html
        """
        if self.boto_client is None:
            self._connect_boto_client()

        try:
            response = self.boto_client.get_pull_request(pullRequestId=str(pr_number))
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == 'PullRequestDoesNotExistException':
                raise ValueError(f"CodeCommit cannot retrieve PR: PR number does not exist: {pr_number}") from e
            if e.response["Error"]["Code"] == 'RepositoryDoesNotExistException':
                raise ValueError(f"CodeCommit cannot retrieve PR: Repository does not exist: {repo_name}") from e
            raise ValueError(f"CodeCommit cannot retrieve PR: {pr_number}: boto client error") from e
        except Exception as e:
            raise ValueError(f"CodeCommit cannot retrieve PR: {pr_number}") from e

        if "pullRequest" not in response:
            raise ValueError("CodeCommit PR number not found: {pr_number}")

        return CodeCommitPullRequestResponse(response.get("pullRequest", {}))

    def publish_description(self, pr_number: int, pr_title: str, pr_body: str):
        """
        Set the title and description on a pull request

        Args:
        - pr_number: the AWS CodeCommit pull request number
        - pr_title: title of the pull request
        - pr_body: body of the pull request

        Returns:
        - None

        Boto3 Documentation:
        - aws codecommit update_pull_request_title
        - https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/codecommit/client/update_pull_request_title.html
        - aws codecommit update_pull_request_description
        - https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/codecommit/client/update_pull_request_description.html
        """
        if self.boto_client is None:
            self._connect_boto_client()

        try:
            if pr_title is not None:
                self.boto_client.update_pull_request_title(pullRequestId=str(pr_number), title=pr_title)
            self.boto_client.update_pull_request_description(pullRequestId=str(pr_number), description=pr_body)
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == 'PullRequestDoesNotExistException':
                raise ValueError(f"PR number does not exist: {pr_number}") from e
            if e.response["Error"]["Code"] == 'InvalidTitleException':
                raise ValueError(f"Invalid title for PR number: {pr_number}") from e
            if e.response["Error"]["Code"] == 'InvalidDescriptionException':
                raise ValueError(f"Invalid description for PR number: {pr_number}") from e
            if e.response["Error"]["Code"] == 'PullRequestAlreadyClosedException':
                raise ValueError(f"PR is already closed: PR number: {pr_number}") from e
            raise ValueError(f"Boto3 client error calling publish_description") from e
        except Exception as e:
            raise ValueError(f"Error calling publish_description") from e

    def publish_comment(self, repo_name: str, pr_number: int, destination_commit: str, source_commit: str, comment: str, annotation_file: str = None, annotation_line: int = None):
        """
        Publish a comment to a pull request

        Args:
        - repo_name: name of the repository
        - pr_number: number of the pull request
        - destination_commit: The commit hash you want to merge into (the "before" hash) (usually on the main or master branch)
        - source_commit: The commit hash of the code you are adding (the "after" branch)
        - comment: The comment you want to publish
        - annotation_file: The file you want to annotate (optional)
        - annotation_line: The line number you want to annotate (optional)

        Comment annotations for CodeCommit are different than GitHub.
        CodeCommit only designates the starting line number for the comment.
        It does not support the ending line number to highlight a range of lines.

        Returns:
        - None

        Boto3 Documentation:
        - aws codecommit post_comment_for_pull_request
        - https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/codecommit/client/post_comment_for_pull_request.html
        """
        if self.boto_client is None:
            self._connect_boto_client()

        try:
            # If the comment has code annotations,
            # then set the file path and line number in the location dictionary
            if annotation_file and annotation_line:
                self.boto_client.post_comment_for_pull_request(
                    pullRequestId=str(pr_number),
                    repositoryName=repo_name,
                    beforeCommitId=destination_commit,
                    afterCommitId=source_commit,
                    content=comment,
                    location={
                        "filePath": annotation_file,
                        "filePosition": annotation_line,
                        "relativeFileVersion": "AFTER",
                    },
                )
            else:
                # The comment does not have code annotations
                self.boto_client.post_comment_for_pull_request(
                    pullRequestId=str(pr_number),
                    repositoryName=repo_name,
                    beforeCommitId=destination_commit,
                    afterCommitId=source_commit,
                    content=comment,
                )
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == 'RepositoryDoesNotExistException':
                raise ValueError(f"Repository does not exist: {repo_name}") from e
            if e.response["Error"]["Code"] == 'PullRequestDoesNotExistException':
                raise ValueError(f"PR number does not exist: {pr_number}") from e
            raise ValueError(f"Boto3 client error calling post_comment_for_pull_request") from e
        except Exception as e:
            raise ValueError(f"Error calling post_comment_for_pull_request") from e
