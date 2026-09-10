"""Run the seeded-defect corpus through /review and score the result.

Needs a live model, so it is deliberately outside ``testpaths`` (``pyproject.toml``) and never
runs in CI. See README.md for invocation.

    PYTHONPATH=. uv run python tests/eval/run_eval.py --out results.json

Every item is driven through the ``plain_diff`` provider: no hosting platform, no network beyond
the model, and the structured review lands in a JSON file the scorer reads.
"""

import argparse
import asyncio
import copy
import json
import os
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from starlette_context import context, request_cycle_context

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.plain_diff_provider import PlainDiffGitProvider
from pr_agent.log import get_logger, setup_logger
from pr_agent.tools.pr_reviewer import PRReviewer
from tests.eval.corpus import ALL_DEFECTS, SeededDefect, reverted_fix_diff
from tests.eval.mutate import generate_mutants
from tests.eval.scoring import DefectResult, score_defect, summarize


class EnrichedCorpusError(RuntimeError):
    """The provider replaced a seeded patch with the real working-tree file."""


def defect_diff(defect: SeededDefect, repo_root: str) -> str:
    if defect.diff_text:
        return defect.diff_text
    return reverted_fix_diff(defect.source.split(":", 1)[1], repo_root=repo_root)


def _assert_patch_only(provider: PlainDiffGitProvider, defect: SeededDefect) -> None:
    """Fail loudly when working-tree enrichment poisoned a seeded defect.

    PlainDiffGitProvider reads the real file whenever a diff path resolves under the repo root
    (plain_diff_provider.py:78). For a reverted-fix item the real file is the *fixed* one, so the
    model would be reviewing a patch against content that already contains the fix - scoring
    something that is not the seeded defect. Silently scoring that is worse than not running.
    """
    enriched = [f.filename for f in provider.get_diff_files() if getattr(f, "head_file", "")]
    if enriched:
        raise EnrichedCorpusError(
            f"{defect.id}: working-tree enrichment replaced {enriched}. Run from a directory "
            f"outside any git checkout (see README.md), so the corpus is reviewed as written."
        )


async def run_review_on_diff(diff: str, ai_handler=None, log_id: str = "eval",
                             on_provider=None) -> dict | None:
    """Drive one unified diff through PRReviewer via PlainDiffGitProvider; return the parsed review.

    Shared by the seeded-defect runner (`run_one`) and the `--labels` real-PR path, so both
    exercise the exact same reviewer construction and JSON read-back.
    """
    with tempfile.TemporaryDirectory() as tmp:
        json_path = os.path.join(tmp, "review.json")
        settings = get_settings()
        settings.set("config.git_provider", "plain_diff")
        settings.set("plain_diff.content", diff)
        settings.set("plain_diff.output_path", os.path.join(tmp, "review.md"))
        settings.set("plain_diff.json_output_path", json_path)

        # Let PRReviewer build the provider from settings rather than constructing a second
        # one: the token handler is built against whichever instance the reviewer holds.
        reviewer = PRReviewer("plain-diff", args=[], **({"ai_handler": ai_handler} if ai_handler else {}))
        if on_provider is not None:
            on_provider(reviewer.git_provider)
        try:
            await reviewer.run()
        except Exception as e:
            # A raised review is a real outcome for this harness (it is what the fallback-chain
            # fix makes visible), not a harness bug - record it as a parse failure and continue,
            # so one bad item does not lose the whole run.
            get_logger().warning(f"{log_id}: review raised {type(e).__name__}: {e}")

        review = None
        if os.path.isfile(json_path):
            try:
                with open(json_path, encoding="utf-8") as fh:
                    review = json.load(fh)
            except (OSError, ValueError) as e:
                get_logger().warning(f"{log_id}: unreadable structured review: {e}")
    return review


async def run_one(defect: SeededDefect, repo_root: str,
                  ai_handler=None) -> tuple[DefectResult, dict | None]:
    diff = defect_diff(defect, repo_root)
    review = await run_review_on_diff(
        diff, ai_handler=ai_handler, log_id=defect.id,
        on_provider=lambda provider: _assert_patch_only(provider, defect),
    )
    return score_defect(defect, review, diff), review


def mine_reverted_fixes(repo_root: str, count: int, max_lines: int = 80) -> list[SeededDefect]:
    """Turn the most recent small `fix:` commits into reverted-fix items.

    Labels are the commit subjects, which is weaker than the hand-curated items in corpus.py -
    there are no wording signals - so these score on line overlap alone. Every fix commit is a
    real bug someone shipped, which no hand-written mutant can claim.
    """
    log = subprocess.run(
        ["git", "-C", repo_root, "log", "--grep=^fix", "-n", str(count * 4), "--format=%h%x09%s"],
        capture_output=True, text=True, check=True).stdout
    defects = []
    for line in log.splitlines():
        sha, _, subject = line.partition("\t")
        diff = reverted_fix_diff(sha, repo_root)
        changed = sum(1 for ln in diff.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---")))
        if not diff.strip() or changed > max_lines:
            continue
        files = tuple(ln[6:] for ln in diff.splitlines() if ln.startswith("+++ b/"))
        defects.append(SeededDefect(
            id=f"mined-{sha}", defect_class="reverted-fix", summary=subject, files=files,
            signals=(), source=f"reverted-fix:{sha}", diff_text=diff))
        if len(defects) >= count:
            break
    return defects


def _apply_overrides(pairs: list[str]) -> dict:
    """`--set pr_reviewer.num_max_findings=12` style overrides, typed like TOML scalars."""
    applied = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise SystemExit(f"--set expects key=value, got {pair!r}")
        value: object = raw
        lowered = raw.strip().lower()
        if lowered in ("true", "false"):
            value = lowered == "true"
        else:
            for cast in (int, float):
                try:
                    value = cast(raw)
                    break
                except ValueError:
                    continue
        get_settings().set(key, value)
        applied[key] = value
    return applied


def _run_metadata(args, overrides: dict) -> dict:
    settings = get_settings()
    head = subprocess.run(["git", "-C", args.repo_root, "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    return {
        "model": settings.config.model,
        "fallback_models": list(settings.config.get("fallback_models", []) or []),
        "temperature": settings.config.temperature,
        "num_samples": settings.pr_reviewer.get("num_samples", 1),
        "min_votes": settings.pr_reviewer.get("min_votes", 2),
        "num_max_findings": settings.pr_reviewer.get("num_max_findings"),
        "response_format": settings.litellm.get("response_format", ""),
        "overrides": overrides,
        "repo_head": head,
        "mutant_seed": args.seed if args.mutants else None,
    }


async def main_async(args) -> int:
    overrides = _apply_overrides(args.set)
    if args.labels:
        if not args.diff_file:
            raise SystemExit("--labels requires --diff-file")
        from tests.eval.labels import load_labels, score_labels
        label_set = load_labels(args.labels)
        diff_text = Path(args.diff_file).read_text()
        review = await run_review_on_diff(diff_text, log_id=f"labels:{label_set.repo}#{label_set.pr}")
        report = score_labels(label_set, review)
        print(json.dumps(report.as_dict(), indent=2))
        if args.out:
            Path(args.out).write_text(json.dumps({"labels": report.as_dict()}, indent=2))
        return 0
    corpus = list(ALL_DEFECTS) if not args.no_curated else []
    if args.mutants:
        corpus += generate_mutants(args.repo_root, args.mutants, args.seed)
    if args.mine_fixes:
        corpus += mine_reverted_fixes(args.repo_root, args.mine_fixes)
    selected = [d for d in corpus if not args.only or d.id in args.only]
    if not selected:
        raise SystemExit("nothing selected")

    results: list[DefectResult] = []
    reviews: dict[str, dict | None] = {}
    timings: dict[str, float] = {}
    for defect in selected:
        get_logger().info(f"eval: {defect.id}")
        original = copy.deepcopy(get_settings())
        started = time.monotonic()
        try:
            result, review = await run_one(defect, args.repo_root)
        finally:
            context["settings"] = copy.deepcopy(original)
        timings[defect.id] = round(time.monotonic() - started, 2)
        results.append(result)
        reviews[defect.id] = review
        print(f"  {defect.id:<48} {result.outcome.value:<10} {timings[defect.id]:>7.1f}s")

    summary = summarize(results)
    summary["wall_clock_seconds"] = round(sum(timings.values()), 1)
    report = {
        "metadata": _run_metadata(args, overrides),
        "summary": summary,
        "results": [asdict(r) | {"outcome": r.outcome.value, "seconds": timings[r.defect_id]}
                    for r in results],
    }
    if args.keep_reviews:
        report["reviews"] = reviews
    print(json.dumps(summary, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"wrote {args.out}")
    # A run where nothing parsed is a failed run, not a zero score.
    return 1 if summary.get("parse_fail_rate") == 1.0 else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", help="write the full JSON report here")
    parser.add_argument("--only", nargs="*", default=[], help="defect ids to run")
    parser.add_argument("--repo-root", default=os.getcwd(),
                        help="checkout to generate reverted-fix diffs and mutants from")
    parser.add_argument("--mutants", type=int, default=0,
                        help="add this many AST-generated mutants of real repo files (see mutate.py)")
    parser.add_argument("--seed", type=int, default=0, help="mutant selection seed; same seed, same corpus")
    parser.add_argument("--mine-fixes", type=int, default=0,
                        help="add this many recent small `fix:` commits as reverted-fix items")
    parser.add_argument("--no-curated", action="store_true", help="skip the hand-curated items in corpus.py")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="override any setting for this run, e.g. --set pr_reviewer.num_samples=3")
    parser.add_argument("--keep-reviews", action="store_true", help="include each raw review in the report")
    parser.add_argument("--labels", help="path to a labeled real-PR JSON (tests/eval/labels/*.json)")
    parser.add_argument("--diff-file", help="unified diff for --labels; fetch with tests/eval/fetch_pr_diff.sh")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args()
    setup_logger(args.log_level)
    with request_cycle_context({}):
        return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
