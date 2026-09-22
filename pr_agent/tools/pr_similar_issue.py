import hashlib
import time
from enum import Enum
from typing import List

import openai
from pydantic import BaseModel, Field

from pr_agent.algo import MAX_TOKENS
from pr_agent.algo.token_budget import get_max_tokens
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider
from pr_agent.log import get_logger

MODEL = "text-embedding-ada-002"
PINECONE_UPSERT_READY_TIMEOUT_SECONDS = 30
PINECONE_UPSERT_READY_POLL_SECONDS = 0.5


_EMBEDDING_CLIENTS = {}


def _get_embedding_client(api_key: str):
    """Return the openai client for this key, reusing its connection pool."""
    if api_key not in _EMBEDDING_CLIENTS:
        _EMBEDDING_CLIENTS[api_key] = openai.OpenAI(api_key=api_key)
    return _EMBEDDING_CLIENTS[api_key]


def _embed(texts: List[str]) -> List[List[float]]:
    """Embed texts with the openai>=1.0 client that requirements.txt pins."""
    client = _get_embedding_client(get_settings().openai.key)
    response = client.embeddings.create(input=texts, model=MODEL)
    return [record.embedding for record in response.data]


def _embed_with_fallback(texts: List[str]) -> List[List[float]]:
    """Embed a list, falling back to one by one, and refuse to return an all-zero set."""
    try:
        return _embed(texts)
    except Exception as e:
        get_logger().error("Failed to embed entire list, embedding one by one...",
                           artifact={"error": str(e)})
        embeds = []
        failures = 0
        for text in texts:
            try:
                embeds.append(_embed([text])[0])
            except Exception:
                failures += 1
                embeds.append([0] * 1536)
        if failures == len(texts):
            raise RuntimeError(
                "Failed to embed any issue text; refusing to index all-zero vectors") from e
        return embeds


def _qdrant_collection_name(base_name: str) -> str:
    """Derive the qdrant collection name from the shared index name.

    Point ids were re-seeded with the repository name in #2323, so points written by an earlier
    version live under ids that are never rewritten and never deleted. Writing the new ids into a
    separate collection keeps those stale points out of every query without deleting anything: the
    pre-#2323 collection is left untouched and can be dropped by hand once it is no longer wanted.

    Only the qdrant backend uses this; ``self.index_name`` is shared with pinecone and lancedb and
    is deliberately left alone.
    """
    return f"{base_name}-v2"


def _lancedb_similar_search(table, query_vector, repo_name_for_index):
    """Search a lancedb table with the same cosine metric and five-hit limit as the other backends.

    Pinecone and qdrant both return cosine similarity and request five hits; lancedb's default
    squared-L2 metric would otherwise make ``1 - _distance`` meaningless. Cosine distance keeps the
    printed score (``1 - _distance``) equal to the cosine similarity reported elsewhere.
    """
    return (
        table.search(query_vector)
        .distance_type("cosine")
        .limit(5)
        .where(f"metadata.repo='{repo_name_for_index}'", prefilter=True)
        .to_list()
    )


def _pinecone_namespace(repo_full_name: str) -> str:
    """Return a collision-resistant Pinecone namespace for a canonical repository name."""
    return f"repo-{hashlib.sha256(repo_full_name.lower().encode()).hexdigest()}"


def _raise_on_pinecone_upsert_errors(response):
    """Raise when Pinecone reports asynchronous batch failures in the upsert response."""
    response_dict = {}
    if isinstance(response, dict):
        response_dict = response
    elif hasattr(response, "to_dict"):
        response_dict = response.to_dict()

    failed_item_count = getattr(response, "failed_item_count", response_dict.get("failed_item_count"))
    has_errors = getattr(response, "has_errors", response_dict.get("has_errors", False))
    if not has_errors and not failed_item_count:
        return

    errors = getattr(response, "errors", response_dict.get("errors")) or []
    get_logger().error(
        "Pinecone upsert failed",
        artifact={
            "failed_item_count": failed_item_count,
            "errors": [_get_value(error, "error_message") for error in errors],
        },
    )
    raise RuntimeError(f"Pinecone upsert failed for {failed_item_count} vectors")


def _get_value(source, key, default=None):
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def _pinecone_response_info(response):
    response_info = _get_value(response, "response_info")
    if response_info is None and hasattr(response, "to_dict"):
        response_info = response.to_dict().get("response_info")
    return response_info


def _pinecone_lsn_committed(response):
    response_info = _pinecone_response_info(response)
    return _get_value(response_info, "lsn_committed")


def _pinecone_response_is_reconciled(response, target_lsn):
    response_info = _pinecone_response_info(response)
    if response_info is None:
        return False
    if hasattr(response_info, "is_reconciled"):
        return response_info.is_reconciled(target_lsn)
    lsn_reconciled = _get_value(response_info, "lsn_reconciled")
    return lsn_reconciled is not None and lsn_reconciled >= target_lsn


def _wait_for_pinecone_upsert_readiness(pinecone_index, upsert_response, namespace, vector_id):
    target_lsn = _pinecone_lsn_committed(upsert_response)
    if target_lsn is None:
        get_logger().warning("Pinecone upsert response did not include an LSN; skipping readiness wait")
        return

    deadline = time.monotonic() + PINECONE_UPSERT_READY_TIMEOUT_SECONDS
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Pinecone upsert was not query-ready after {PINECONE_UPSERT_READY_TIMEOUT_SECONDS}s")
        fetch_response = pinecone_index.fetch(ids=[vector_id], namespace=namespace)
        if _pinecone_response_is_reconciled(fetch_response, target_lsn):
            return
        time.sleep(PINECONE_UPSERT_READY_POLL_SECONDS)


def _provider_supports_issue_indexing() -> bool:
    """Whether the configured provider can back `/similar_issue`.

    The check is on the provider class rather than on the configured id, so a provider
    registered through `register_git_provider()` is judged by the capability it declares.
    An unresolvable configuration is reported as unsupported, which is what the tool's
    `run()` already handles, rather than raised out of `__init__`.
    """
    try:
        provider_class = get_git_provider()
    except ValueError:
        return False
    return provider_class.supports_issue_indexing()


class PRSimilarIssue:
    def __init__(self, issue_url: str, ai_handler, args: list = None):
        self.issue_url = issue_url
        self.supported = _provider_supports_issue_indexing()
        if not self.supported:
            return

        self.cli_mode = get_settings().CONFIG.CLI_MODE
        self.max_issues_to_scan = get_settings().pr_similar_issue.max_issues_to_scan
        self.git_provider = get_git_provider()()
        repo_name, issue_number = self.git_provider._parse_issue_url(issue_url.split('=')[-1])
        self.git_provider.repo = repo_name
        self.git_provider.repo_obj = self.git_provider.github_client.get_repo(repo_name)
        self.token_handler = TokenHandler()
        repo_obj = self.git_provider.repo_obj
        repo_name_for_index = self.repo_name_for_index = repo_obj.full_name.lower().replace('/', '-').replace('_/', '-')
        self.pinecone_namespace = _pinecone_namespace(repo_obj.full_name)
        index_name = self.index_name = "codium-ai-pr-agent-issues"

        if get_settings().pr_similar_issue.vectordb == "pinecone":
            try:
                import pinecone
                from pinecone import ServerlessSpec
            except ImportError:
                raise Exception("Please install the 'pinecone' package to use pinecone as vectordb") from None
            # assuming pinecone api key, cloud and region are set in secrets file
            try:
                api_key = get_settings().pinecone.api_key
                cloud = get_settings().pinecone.cloud
                region = get_settings().pinecone.region
            except Exception:
                if not self.cli_mode:
                    repo_name, original_issue_number = self.git_provider._parse_issue_url(self.issue_url.split('=')[-1])
                    issue_main = self.git_provider.repo_obj.get_issue(original_issue_number)
                    issue_main.create_comment("Please set pinecone api key, cloud and region in secrets file")
                raise Exception("Please set pinecone api key, cloud and region in secrets file")
            self.pc = pinecone.Pinecone(api_key=api_key)
            self.pc_spec = ServerlessSpec(cloud=cloud, region=region)
            self.pinecone_index = None

            # check if index exists, and if repo is already indexed
            run_from_scratch = False
            upsert = True
            if not self.pc.has_index(index_name):
                run_from_scratch = True
                upsert = False
            else:
                if get_settings().pr_similar_issue.force_update_dataset:
                    upsert = True
                else:
                    self.pinecone_index = self.pc.Index(name=index_name)
                    res = self.pinecone_index.fetch(
                        ids=[f"example_issue_{repo_name_for_index}"],
                        namespace=self.pinecone_namespace,
                    ).to_dict()
                    if res["vectors"]:
                        upsert = False

            if run_from_scratch or upsert:  # index the entire repo
                get_logger().info('Indexing the entire repo...')

                get_logger().info('Getting issues...')
                issues = list(repo_obj.get_issues(state='all'))
                get_logger().info('Done')
                self._update_index_with_issues(
                    issues,
                    repo_name_for_index,
                    pinecone_namespace=self.pinecone_namespace,
                    upsert=upsert,
                )
            else:  # update index if needed
                self.pinecone_index = self.pc.Index(name=index_name)
                issues_to_update = []
                issues_paginated_list = repo_obj.get_issues(state='all')
                for issue in issues_paginated_list:
                    if issue.pull_request:
                        continue
                    issue_str, comments, number = self._process_issue(issue)
                    issue_key = f"issue_{number}"
                    id = issue_key + "." + "issue"
                    res = self.pinecone_index.fetch(ids=[id], namespace=self.pinecone_namespace).to_dict()
                    is_new_issue = True
                    for vector in res["vectors"].values():
                        if vector['metadata']['repo'] == repo_name_for_index:
                            is_new_issue = False
                            break
                    if is_new_issue:
                        issues_to_update.append(issue)
                    else:
                        break

                if issues_to_update:
                    get_logger().info(f'Updating index with {len(issues_to_update)} new issues...')
                    self._update_index_with_issues(
                        issues_to_update,
                        repo_name_for_index,
                        pinecone_namespace=self.pinecone_namespace,
                        upsert=True,
                    )
                else:
                    get_logger().info('No new issues to update')

        elif get_settings().pr_similar_issue.vectordb == "lancedb":
            try:
                import lancedb  # import lancedb only if needed
            except:
                raise Exception("Please install lancedb to use lancedb as vectordb")
            self.db = lancedb.connect(get_settings().lancedb.uri)
            self.table = None

            run_from_scratch = False
            ingest = True
            force_refresh = False
            if not self._table_exists_in_db(index_name):
                run_from_scratch = True
                ingest = False
            else:
                if get_settings().pr_similar_issue.force_update_dataset:
                    force_refresh = True
                    ingest = True
                else:
                    self.table = self.db[index_name]
                    if self._lancedb_repo_already_indexed(repo_name_for_index):
                        ingest = False
                    else:
                        force_refresh = True

            if run_from_scratch or ingest:  # indexing the entire repo
                get_logger().info('Indexing the entire repo...')

                get_logger().info('Getting issues...')
                issues = list(repo_obj.get_issues(state='all'))
                get_logger().info('Done')

                self._update_table_with_issues(issues, repo_name_for_index, ingest=ingest, force_refresh=force_refresh)
            else:  # update table if needed
                issues_to_update = []
                issues_paginated_list = repo_obj.get_issues(state='all')
                for issue in issues_paginated_list:
                    if issue.pull_request:
                        continue
                    issue_str, comments, number = self._process_issue(issue)
                    issue_key = f"issue_{number}"
                    issue_id = issue_key + "." + "issue"
                    res = self.table.search().limit(len(self.table)).where(f"id='{issue_id}'").to_list()
                    is_new_issue = True
                    for r in res:
                        if r['metadata']['repo'] == repo_name_for_index:
                            is_new_issue = False
                            break
                    if is_new_issue:
                        issues_to_update.append(issue)
                    else:
                        break

                if issues_to_update:
                    get_logger().info(f'Updating index with {len(issues_to_update)} new issues...')
                    self._update_table_with_issues(issues_to_update, repo_name_for_index, ingest=True)
                else:
                    get_logger().info('No new issues to update')

        elif get_settings().pr_similar_issue.vectordb == "qdrant":
            try:
                import qdrant_client
                from qdrant_client.models import Distance, FieldCondition, Filter, MatchValue, VectorParams
            except Exception:
                raise Exception("Please install qdrant-client to use qdrant as vectordb")

            # Scoped to qdrant only: pinecone and lancedb keep using self.index_name unchanged.
            self.qdrant_collection_name = _qdrant_collection_name(index_name)

            api_key = None
            url = None
            try:
                api_key = get_settings().qdrant.api_key
                url = get_settings().qdrant.url
            except Exception:
                url = None

            if not url:
                if not self.cli_mode:
                    repo_name, original_issue_number = self.git_provider._parse_issue_url(self.issue_url.split('=')[-1])
                    issue_main = self.git_provider.repo_obj.get_issue(original_issue_number)
                    issue_main.create_comment("Please set qdrant url and api key in secrets file")
                raise Exception("Please set qdrant url and api key in secrets file")

            self.qdrant = qdrant_client.QdrantClient(url=url, api_key=api_key)

            run_from_scratch = False
            ingest = True

            if not self.qdrant.collection_exists(collection_name=self.qdrant_collection_name):
                run_from_scratch = True
                ingest = False
                self.qdrant.create_collection(
                    collection_name=self.qdrant_collection_name,
                    vectors_config=VectorParams(size=1536, distance=Distance.COSINE),
                )
            else:
                if get_settings().pr_similar_issue.force_update_dataset:
                    ingest = True
                else:
                    response = self.qdrant.count(
                        collection_name=self.qdrant_collection_name,
                        count_filter=Filter(must=[
                            FieldCondition(key="metadata.repo", match=MatchValue(value=repo_name_for_index)),
                            FieldCondition(key="id", match=MatchValue(value=f"example_issue_{repo_name_for_index}")),
                        ]),
                    )
                    ingest = True if response.count == 0 else False

            if run_from_scratch or ingest:
                get_logger().info('Indexing the entire repo...')
                get_logger().info('Getting issues...')
                issues = list(repo_obj.get_issues(state='all'))
                get_logger().info('Done')
                self._update_qdrant_with_issues(issues, repo_name_for_index, ingest=ingest)
            else:
                issues_to_update = []
                issues_paginated_list = repo_obj.get_issues(state='all')
                for issue in issues_paginated_list:
                    if issue.pull_request:
                        continue
                    issue_str, comments, number = self._process_issue(issue)
                    issue_key = f"issue_{number}"
                    point_id = issue_key + "." + "issue"
                    response = self.qdrant.count(
                        collection_name=self.qdrant_collection_name,
                        count_filter=Filter(must=[
                            FieldCondition(key="id", match=MatchValue(value=point_id)),
                            FieldCondition(key="metadata.repo", match=MatchValue(value=repo_name_for_index)),
                        ]),
                    )
                    if response.count == 0:
                        issues_to_update.append(issue)
                    else:
                        break

                if issues_to_update:
                    get_logger().info(f'Updating index with {len(issues_to_update)} new issues...')
                    self._update_qdrant_with_issues(issues_to_update, repo_name_for_index, ingest=True)
                else:
                    get_logger().info('No new issues to update')


    @staticmethod
    def _record_similar_hit(relevant_issues_number_list: list, relevant_comment_number_list: list,
                            score_list: list, issue_number: int, comment_number: int, score: float):
        """Keep the first, best-scored hit per issue and keep the three lists aligned."""
        if issue_number in relevant_issues_number_list:
            return
        relevant_issues_number_list.append(issue_number)
        relevant_comment_number_list.append(comment_number)
        score_list.append(str("{:.2f}".format(score)))

    async def run(self):
        if not self.supported:
            message = "The /similar_issue tool is not supported by the configured git provider."
            if get_settings().config.publish_output:
                try:
                    from pr_agent.git_providers import get_git_provider_with_context

                    provider = get_git_provider_with_context(self.issue_url)
                    provider.publish_comment(message)
                except Exception as e:
                    get_logger().warning(
                        "Failed to publish /similar_issue unsupported message",
                        artifact={"error": str(e)},
                    )
            return ""

        get_logger().info('Getting issue...')
        repo_name, original_issue_number = self.git_provider._parse_issue_url(self.issue_url.split('=')[-1])
        issue_main = self.git_provider.repo_obj.get_issue(original_issue_number)
        issue_str, comments, number = self._process_issue(issue_main)
        get_logger().info('Done')

        get_logger().info('Querying...')
        embeds = _embed([issue_str])

        relevant_issues_number_list = []
        relevant_comment_number_list = []
        score_list = []

        if get_settings().pr_similar_issue.vectordb == "pinecone":
            pinecone_index = self.pc.Index(name=self.index_name)
            res = pinecone_index.query(vector=embeds[0],
                                    top_k=5,
                                    filter={"repo": self.repo_name_for_index},
                                    namespace=self.pinecone_namespace,
                                    include_metadata=True).to_dict()

            for r in res['matches']:
                # skip example issue
                if 'example_issue_' in r["id"]:
                    continue

                try:
                    issue_number = int(r["id"].split('.')[0].split('_')[-1])
                except:
                    get_logger().debug(f"Failed to parse issue number from {r['id']}")
                    continue

                if original_issue_number == issue_number:
                    continue
                comment_number = int(r["id"].split('.')[1].split('_')[-1]) if 'comment' in r["id"] else -1
                self._record_similar_hit(relevant_issues_number_list, relevant_comment_number_list,
                                         score_list, issue_number, comment_number, r['score'])
            get_logger().info('Done')

        elif get_settings().pr_similar_issue.vectordb == "lancedb":
            res = _lancedb_similar_search(self.table, embeds[0], self.repo_name_for_index)

            for r in res:
                # skip example issue
                if 'example_issue_' in r["id"]:
                    continue

                try:
                    issue_number = int(r["id"].split('.')[0].split('_')[-1])
                except:
                    get_logger().debug(f"Failed to parse issue number from {r['id']}")
                    continue

                if original_issue_number == issue_number:
                    continue
                comment_number = int(r["id"].split('.')[1].split('_')[-1]) if 'comment' in r["id"] else -1
                self._record_similar_hit(relevant_issues_number_list, relevant_comment_number_list,
                                         score_list, issue_number, comment_number, 1 - r['_distance'])
            get_logger().info('Done')

        elif get_settings().pr_similar_issue.vectordb == "qdrant":
            from qdrant_client.models import FieldCondition, Filter, MatchValue
            res = self.qdrant.search(
                collection_name=self.qdrant_collection_name,
                query_vector=embeds[0],
                limit=5,
                query_filter=Filter(
                    must=[FieldCondition(key="metadata.repo", match=MatchValue(value=self.repo_name_for_index))]
                ),
                with_payload=True,
            )

            for r in res:
                rid = r.payload.get("id", "")
                if 'example_issue_' in rid:
                    continue
                try:
                    issue_number = int(rid.split('.')[0].split('_')[-1])
                except Exception:
                    get_logger().debug(f"Failed to parse issue number from {rid}")
                    continue
                if original_issue_number == issue_number:
                    continue
                comment_number = int(rid.split('.')[1].split('_')[-1]) if 'comment' in rid else -1
                self._record_similar_hit(relevant_issues_number_list, relevant_comment_number_list,
                                         score_list, issue_number, comment_number, r.score)
            get_logger().info('Done')

        get_logger().info('Publishing response...')
        similar_issues_str = "### Similar Issues\n___\n\n"

        for i, issue_number_similar in enumerate(relevant_issues_number_list):
            issue = self.git_provider.repo_obj.get_issue(issue_number_similar)
            title = issue.title
            url = issue.html_url
            if relevant_comment_number_list[i] != -1:
                url = list(issue.get_comments())[relevant_comment_number_list[i]].html_url
            similar_issues_str += f"{i + 1}. **[{title}]({url})** (score={score_list[i]})\n\n"
        if get_settings().config.publish_output:
            issue_main.create_comment(similar_issues_str)
        get_logger().info(similar_issues_str)
        get_logger().info("Done")

    def _process_issue(self, issue):
        header = issue.title
        body = issue.body
        number = issue.number
        if get_settings().pr_similar_issue.skip_comments:
            comments = []
        else:
            comments = list(issue.get_comments())
        issue_str = f"Issue Header: \"{header}\"\n\nIssue Body:\n{body}"
        return issue_str, comments, number

    def _update_index_with_issues(self, issues_list, repo_name_for_index, pinecone_namespace, upsert=False):
        import pandas as pd

        get_logger().info('Processing issues...')
        corpus = Corpus()
        example_issue_record = Record(
            id=f"example_issue_{repo_name_for_index}",
            text="example_issue",
            metadata=Metadata(repo=repo_name_for_index)
        )
        corpus.append(example_issue_record)

        counter = 0
        for issue in issues_list:
            if issue.pull_request:
                continue

            if counter >= self.max_issues_to_scan:
                get_logger().info(f"Scanned {self.max_issues_to_scan} issues, stopping")
                break

            counter += 1
            if counter % 100 == 0:
                get_logger().info(f"Scanned {counter} issues")

            issue_str, comments, number = self._process_issue(issue)
            issue_key = f"issue_{number}"
            username = issue.user.login
            created_at = str(issue.created_at)
            if len(issue_str) < 8000 or \
                    self.token_handler.count_tokens(issue_str) < get_max_tokens(MODEL):  # fast reject first
                issue_record = Record(
                    id=issue_key + "." + "issue",
                    text=issue_str,
                    metadata=Metadata(repo=repo_name_for_index,
                                      username=username,
                                      created_at=created_at,
                                      level=IssueLevel.ISSUE)
                )
                corpus.append(issue_record)
                if comments:
                    for j, comment in enumerate(comments):
                        comment_body = comment.body
                        if not isinstance(comment_body, str) or len(comment_body.split()) < 10:
                            continue

                        if len(comment_body) < 8000 or \
                                self.token_handler.count_tokens(comment_body) < MAX_TOKENS[MODEL]:
                            comment_record = Record(
                                id=issue_key + ".comment_" + str(j),
                                text=comment_body,
                                metadata=Metadata(repo=repo_name_for_index,
                                                  username=username,  # use issue username for all comments
                                                  created_at=created_at,
                                                  level=IssueLevel.COMMENT)
                            )
                            corpus.append(comment_record)
        df = pd.DataFrame(corpus.model_dump()["documents"])
        get_logger().info('Done')

        get_logger().info('Embedding...')
        list_to_encode = df["text"].to_list()
        embeds = _embed_with_fallback(list_to_encode)
        df["values"] = embeds
        get_logger().info('Done')

        vectors = [
            (row["id"], row["values"], row["metadata"])
            for row in df.to_dict(orient="records")
        ]
        if not upsert:
            get_logger().info('Creating index from scratch...')
            self.pc.create_index(name=self.index_name, dimension=len(embeds[0]), metric="cosine", spec=self.pc_spec,
                                 timeout=120)
        get_logger().info('Upserting index...')
        self.pinecone_index = self.pc.Index(name=self.index_name)
        upsert_response = self.pinecone_index.upsert(vectors=vectors,
                                                     namespace=pinecone_namespace,
                                                     batch_size=100,
                                                     max_concurrency=10)
        _raise_on_pinecone_upsert_errors(upsert_response)
        _wait_for_pinecone_upsert_readiness(self.pinecone_index, upsert_response,
                                            namespace=pinecone_namespace, vector_id=vectors[0][0])
        get_logger().info('Done')

    def _table_exists_in_db(self, index_name) -> bool:
        return index_name in self.db.list_tables().tables

    def _lancedb_repo_already_indexed(self, repo_name_for_index) -> bool:
        """Check whether the shared lancedb table already holds this repo's sentinel row.

        One sentinel row per repository is written on the first full ingest, so the row's
        absence on an existing table means this repository has never been indexed and must
        go through the full ingest path to join the table.
        """
        res = self.table.search().limit(len(self.table)).where(
            f"id='example_issue_{repo_name_for_index}'"
        ).to_list()
        get_logger().info("result: ", res)
        return bool(res)

    def _update_table_with_issues(self, issues_list, repo_name_for_index, ingest=False,
                                  force_refresh=False):
        import pandas as pd

        get_logger().info('Processing issues...')

        corpus = Corpus()
        sentinel_id = f"example_issue_{repo_name_for_index}"

        counter = 0
        for issue in issues_list:
            if issue.pull_request:
                continue

            if counter >= self.max_issues_to_scan:
                get_logger().info(f"Scanned {self.max_issues_to_scan} issues, stopping")
                break

            counter += 1
            if counter % 100 == 0:
                get_logger().info(f"Scanned {counter} issues")

            issue_str, comments, number = self._process_issue(issue)
            issue_key = f"issue_{number}"
            username = issue.user.login
            created_at = str(issue.created_at)
            if len(issue_str) < 8000 or \
                    self.token_handler.count_tokens(issue_str) < get_max_tokens(MODEL):  # fast reject first
                issue_record = Record(
                    id=issue_key + "." + "issue",
                    text=issue_str,
                    metadata=Metadata(repo=repo_name_for_index,
                                        username=username,
                                        created_at=created_at,
                                        level=IssueLevel.ISSUE)
                )
                corpus.append(issue_record)
                if comments:
                    for j, comment in enumerate(comments):
                        comment_body = comment.body
                        if not isinstance(comment_body, str) or len(comment_body.split()) < 10:
                            continue

                        if len(comment_body) < 8000 or \
                                self.token_handler.count_tokens(comment_body) < MAX_TOKENS[MODEL]:
                            comment_record = Record(
                                id=issue_key + ".comment_" + str(j),
                                text=comment_body,
                                metadata=Metadata(repo=repo_name_for_index,
                                                    username=username,  # use issue username for all comments
                                                    created_at=created_at,
                                                    level=IssueLevel.COMMENT)
                            )
                            corpus.append(comment_record)

        if len(corpus.documents) == 0:
            if ingest and not force_refresh:
                get_logger().info('No issues to index, skipping update')
                return
            # From-scratch runs and forced refreshes still carry the sentinel row, so the
            # table keeps a searchable marker and the subsequent query path has an index.
            corpus.append(Record(id=sentinel_id, text="example_issue",
                                 metadata=Metadata(repo=repo_name_for_index)))
        else:
            # lancedb add() does not de-duplicate rows, so only carry the sentinel row when
            # the table will not already have one after this write.
            add_sentinel = not ingest or force_refresh
            if ingest and self._table_exists_in_db(self.index_name):
                if self.table is None:
                    self.table = self.db[self.index_name]
                if not force_refresh:
                    add_sentinel = not self.table.search().limit(1).where(f"id='{sentinel_id}'").to_list()
            if add_sentinel:
                corpus.append(Record(id=sentinel_id, text="example_issue",
                                     metadata=Metadata(repo=repo_name_for_index)))

        df = pd.DataFrame(corpus.model_dump()["documents"])
        get_logger().info('Done')

        get_logger().info('Embedding...')
        list_to_encode = df["text"].to_list()
        embeds = _embed_with_fallback(list_to_encode)
        df["vector"] = embeds
        get_logger().info('Done')

        if not ingest:
            get_logger().info('Creating table from scratch...')
            self.table = self.db.create_table(self.index_name, data=df, mode="overwrite")
        else:
            get_logger().info('Ingesting in Table...')
            if self._table_exists_in_db(self.index_name):
                if self.table is None:
                    self.table = self.db[self.index_name]
                if force_refresh:
                    self.table.delete(f"metadata.repo='{repo_name_for_index}'")
                self.table.add(df)
            else:
                get_logger().info(f"Table {self.index_name} doesn't exists!")
        get_logger().info('Done')


    def _update_qdrant_with_issues(self, issues_list, repo_name_for_index, ingest=False):
        try:
            import uuid

            import pandas as pd
            from qdrant_client.models import PointStruct
        except Exception:
            raise

        get_logger().info('Processing issues...')
        corpus = Corpus()
        example_issue_record = Record(
            id=f"example_issue_{repo_name_for_index}",
            text="example_issue",
            metadata=Metadata(repo=repo_name_for_index)
        )
        corpus.append(example_issue_record)

        counter = 0
        for issue in issues_list:
            if issue.pull_request:
                continue

            if counter >= self.max_issues_to_scan:
                get_logger().info(f"Scanned {self.max_issues_to_scan} issues, stopping")
                break

            counter += 1
            if counter % 100 == 0:
                get_logger().info(f"Scanned {counter} issues")

            issue_str, comments, number = self._process_issue(issue)
            issue_key = f"issue_{number}"
            username = issue.user.login
            created_at = str(issue.created_at)
            if len(issue_str) < 8000 or \
                    self.token_handler.count_tokens(issue_str) < get_max_tokens(MODEL):
                issue_record = Record(
                    id=issue_key + "." + "issue",
                    text=issue_str,
                    metadata=Metadata(repo=repo_name_for_index,
                                      username=username,
                                      created_at=created_at,
                                      level=IssueLevel.ISSUE)
                )
                corpus.append(issue_record)
                if comments:
                    for j, comment in enumerate(comments):
                        comment_body = comment.body
                        if not isinstance(comment_body, str) or len(comment_body.split()) < 10:
                            continue

                        if len(comment_body) < 8000 or \
                                self.token_handler.count_tokens(comment_body) < MAX_TOKENS[MODEL]:
                            comment_record = Record(
                                id=issue_key + ".comment_" + str(j),
                                text=comment_body,
                                metadata=Metadata(repo=repo_name_for_index,
                                                  username=username,
                                                  created_at=created_at,
                                                  level=IssueLevel.COMMENT)
                            )
                            corpus.append(comment_record)

        df = pd.DataFrame(corpus.model_dump()["documents"])
        get_logger().info('Done')

        get_logger().info('Embedding...')
        list_to_encode = df["text"].to_list()
        embeds = _embed_with_fallback(list_to_encode)
        df["vector"] = embeds
        get_logger().info('Done')

        get_logger().info('Upserting into Qdrant...')
        points = []
        for row in df.to_dict(orient="records"):
            point_uuid = uuid.uuid5(
                uuid.NAMESPACE_DNS,
                f"{repo_name_for_index}:{row['id']}",
            ).hex
            points.append(
                PointStruct(
                    id=point_uuid,
                    vector=row["vector"],
                    payload={
                        "id": row["id"],
                        "text": row["text"],
                        "metadata": row["metadata"],
                    },
                )
            )
        self.qdrant.upsert(collection_name=self.qdrant_collection_name, points=points)
        get_logger().info('Done')


class IssueLevel(str, Enum):
    ISSUE = "issue"
    COMMENT = "comment"


class Metadata(BaseModel):
    repo: str
    username: str = Field(default="@codium")
    created_at: str = Field(default="01-01-1970 00:00:00.00000")
    level: IssueLevel = Field(default=IssueLevel.ISSUE)

    class Config:
        use_enum_values = True


class Record(BaseModel):
    id: str
    text: str
    metadata: Metadata


class Corpus(BaseModel):
    documents: List[Record] = Field(default=[])

    def append(self, r: Record):
        self.documents.append(r)
