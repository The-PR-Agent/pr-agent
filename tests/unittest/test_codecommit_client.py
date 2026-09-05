from unittest.mock import MagicMock

from pr_agent.git_providers.codecommit_client import CodeCommitClient


class TestCodeCommitProvider:
    def test_get_differences(self):
        # Create a mock CodeCommitClient instance and codecommit_client member
        api = CodeCommitClient()
        api.boto_client = MagicMock()

        # Mock the response from the AWS client for get_differences method
        api.boto_client.get_paginator.return_value.paginate.return_value = [
            {
                "differences": [
                    {
                        "beforeBlob": {
                            "path": "file1.py",
                            "blobId": "291b15c3ab4219e43a5f4f9091e5a97ee9d7400b",
                        },
                        "afterBlob": {
                            "path": "file1.py",
                            "blobId": "46ad86582da03cc34c804c24b17976571bca1eba",
                        },
                        "changeType": "M",
                    },
                    {
                        "beforeBlob": {"path": "", "blobId": ""},
                        "afterBlob": {
                            "path": "file2.py",
                            "blobId": "2404c7874fcbd684d6779c1420072f088647fd79",
                        },
                        "changeType": "A",
                    },
                    {
                        "beforeBlob": {
                            "path": "file3.py",
                            "blobId": "9af7989045ce40e9478ebb8089dfbadac19a9cde",
                        },
                        "afterBlob": {"path": "", "blobId": ""},
                        "changeType": "D",
                    },
                    {
                        "beforeBlob": {
                            "path": "file5.py",
                            "blobId": "738e36eec120ef9d6393a149252698f49156d5b4",
                        },
                        "afterBlob": {
                            "path": "file6.py",
                            "blobId": "faecdb85f7ba199df927a783b261378a1baeca85",
                        },
                        "changeType": "R",
                    },
                ]
            }
        ]

        diffs = api.get_differences("my_test_repo", "commit1", "commit2")

        assert len(diffs) == 4
        assert diffs[0].before_blob_path == "file1.py"
        assert diffs[0].before_blob_id == "291b15c3ab4219e43a5f4f9091e5a97ee9d7400b"
        assert diffs[0].after_blob_path == "file1.py"
        assert diffs[0].after_blob_id == "46ad86582da03cc34c804c24b17976571bca1eba"
        assert diffs[0].change_type == "M"
        assert diffs[1].before_blob_path == ""
        assert diffs[1].before_blob_id == ""
        assert diffs[1].after_blob_path == "file2.py"
        assert diffs[1].after_blob_id == "2404c7874fcbd684d6779c1420072f088647fd79"
        assert diffs[1].change_type == "A"
        assert diffs[2].before_blob_path == "file3.py"
        assert diffs[2].before_blob_id == "9af7989045ce40e9478ebb8089dfbadac19a9cde"
        assert diffs[2].after_blob_path == ""
        assert diffs[2].after_blob_id == ""
        assert diffs[2].change_type == "D"
        assert diffs[3].before_blob_path == "file5.py"
        assert diffs[3].before_blob_id == "738e36eec120ef9d6393a149252698f49156d5b4"
        assert diffs[3].after_blob_path == "file6.py"
        assert diffs[3].after_blob_id == "faecdb85f7ba199df927a783b261378a1baeca85"
        assert diffs[3].change_type == "R"

    def test_get_file(self):
        # Create a mock CodeCommitClient instance and codecommit_client member
        api = CodeCommitClient()
        api.boto_client = MagicMock()

        # Mock the response from the AWS client for get_pull_request method
        # def get_file(self, repo_name: str, file_path: str, sha_hash: str):
        api.boto_client.get_file.return_value = {
            "commitId": "6335d6d4496e8d50af559560997604bb03abc122",
            "blobId": "c172209495d7968a8fdad76469564fb708460bc1",
            "filePath": "requirements.txt",
            "fileSize": 65,
            "fileContent": b"boto3==1.28.25\ndynaconf==3.1.12\nfastapi==0.99.0\nPyGithub==1.59.*\n",
        }

        repo_name = "my_test_repo"
        file_path = "requirements.txt"
        sha_hash = "84114a356ece1e5b7637213c8e486fea7c254656"
        content = api.get_file(repo_name, file_path, sha_hash)

        assert len(content) == 65
        assert content == b"boto3==1.28.25\ndynaconf==3.1.12\nfastapi==0.99.0\nPyGithub==1.59.*\n"
        assert content.decode("utf-8") == "boto3==1.28.25\ndynaconf==3.1.12\nfastapi==0.99.0\nPyGithub==1.59.*\n"

    def test_get_pr(self):
        # Create a mock CodeCommitClient instance and codecommit_client member
        api = CodeCommitClient()
        api.boto_client = MagicMock()

        # Mock the response from the AWS client for get_pull_request method
        api.boto_client.get_pull_request.return_value = {
            "pullRequest": {
                "pullRequestId": "321",
                "title": "My PR",
                "description": "My PR description",
                "pullRequestTargets": [
                    {
                        "sourceCommit": "commit1",
                        "sourceReference": "branch1",
                        "destinationCommit": "commit2",
                        "destinationReference": "branch2",
                        "repositoryName": "my_test_repo",
                    }
                ],
            }
        }

        pr = api.get_pr("my_test_repo", 321)

        assert pr.title == "My PR"
        assert pr.description == "My PR description"
        assert len(pr.targets) == 1
        assert pr.targets[0].repository_name == "my_test_repo"
        assert pr.targets[0].source_commit == "commit1"
        assert pr.targets[0].source_branch == "branch1"
        assert pr.targets[0].destination_commit == "commit2"
        assert pr.targets[0].destination_branch == "branch2"

    def test_get_pr_preserves_all_target_repositories(self):
        api = CodeCommitClient()
        api.boto_client = MagicMock()
        api.boto_client.get_pull_request.return_value = {
            "pullRequest": {
                "title": "Multi-target PR",
                "pullRequestTargets": [
                    {
                        "repositoryName": "repo-one",
                        "sourceCommit": "source-one",
                        "destinationCommit": "destination-one",
                    },
                    {
                        "repositoryName": "repo-two",
                        "sourceCommit": "source-two",
                        "destinationCommit": "destination-two",
                    },
                ],
            }
        }

        pr = api.get_pr("repo-one", 321)

        assert [(target.repository_name, target.source_commit, target.destination_commit) for target in pr.targets] == [
            ("repo-one", "source-one", "destination-one"),
            ("repo-two", "source-two", "destination-two"),
        ]
