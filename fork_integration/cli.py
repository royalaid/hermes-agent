"""Command-line entry point for fork integration audit and finalization."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .audit import audit_manifest, audit_release_candidate
from .finalize import (
    ReplacementFinalizationBlocked,
    finalize_component_replacement,
)
from .manifest import load_manifest, migrate_schema_1
from .prepare import PreparationBlocked, PreparationFailed, prepare_worktree
from .publish import PublicationBlocked, PublicationFailed, publish_release_candidate


DEFAULT_MANIFEST = Path(__file__).with_name("hermes-fork-manifest.v2.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and inspect the version-controlled fork integration manifest."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="report identities and audit findings")
    mode.add_argument("--audit", action="store_true", help="alias for --status")
    mode.add_argument(
        "--migrate-schema-1",
        metavar="PATH",
        help="print a review-required schema-v2 draft to stdout",
    )
    mode.add_argument(
        "--prepare-worktree",
        metavar="PATH",
        type=Path,
        help="reconstruct the verified ordered patch stack in a new standalone clone",
    )
    mode.add_argument(
        "--release-candidate",
        metavar="FULL_SHA",
        help="strictly audit the canonical manifest blob at an exact candidate commit",
    )
    mode.add_argument(
        "--publish-candidate",
        metavar="FULL_SHA",
        help="publish the candidate's declared head under an exact expected-old lease",
    )
    mode.add_argument(
        "--finalize-component",
        metavar="COMPONENT_ID",
        help="print a manifest with a review-required component safely finalized",
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--installed-repo", type=Path)
    parser.add_argument("--offline", action="store_true", help="skip git ls-remote observations")
    parser.add_argument(
        "--expected-old",
        metavar="FULL_SHA",
        help="exact currently published SHA required by --publish-candidate",
    )
    parser.add_argument(
        "--source-ref",
        metavar="REF",
        help="independent full source ref required by --finalize-component",
    )
    parser.add_argument(
        "--replacement",
        action="append",
        default=[],
        metavar="SOURCE_SHA:INTEGRATION_SHA:ROLE[:RELATED_TO]",
        help="ordered immutable replacement identity; repeat for each coherent patch",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicitly request the already read-only audit path",
    )
    parser.add_argument("--json", action="store_true", help="emit one JSON document")
    return parser


def _parse_replacement(value: str) -> dict[str, str]:
    fields = value.split(":")
    if len(fields) not in {3, 4} or any(not field for field in fields):
        raise ValueError(
            "--replacement must be "
            "SOURCE_SHA:INTEGRATION_SHA:ROLE[:RELATED_TO]"
        )
    replacement = {
        "source_commit": fields[0],
        "integration_commit": fields[1],
        "role": fields[2],
    }
    if len(fields) == 4:
        replacement["related_to"] = fields[3]
    return replacement


def _print_human(report: dict) -> None:
    print(f"ready: {str(report['ready']).lower()}")
    for name, identity in report["identities"].items():
        print(
            f"{name}: commit={identity['commit']} ref={identity['ref']} "
            f"repository={identity['repository']}"
        )
    for finding in report["findings"]:
        location = finding.get("component") or "manifest"
        if finding.get("patch") is not None:
            location += f"[{finding['patch']}]"
        print(f"{finding['severity']}: {finding['code']}: {location}: {finding['message']}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not args.finalize_component and (
            args.source_ref is not None or args.replacement
        ):
            raise ValueError(
                "--source-ref and --replacement are valid only with "
                "--finalize-component"
            )

        if args.publish_candidate:
            incompatible = []
            if args.manifest is not None:
                incompatible.append("--manifest")
            if args.offline:
                incompatible.append("--offline")
            if args.installed_repo is not None:
                incompatible.append("--installed-repo")
            if args.dry_run:
                incompatible.append("--dry-run")
            if incompatible:
                raise ValueError(
                    "publication mode forbids override options: "
                    + ", ".join(incompatible)
                )
            if args.expected_old is None:
                raise ValueError("--publish-candidate requires --expected-old FULL_SHA")
            receipt = publish_release_candidate(
                args.repo, args.publish_candidate, args.expected_old
            )
            print(json.dumps(receipt, indent=2, sort_keys=True))
            return 0

        if args.expected_old is not None:
            raise ValueError("--expected-old is valid only with --publish-candidate")

        if args.finalize_component:
            incompatible = []
            if args.offline:
                incompatible.append("--offline")
            if args.installed_repo is not None:
                incompatible.append("--installed-repo")
            if args.dry_run:
                incompatible.append("--dry-run")
            if incompatible:
                raise ValueError(
                    "component finalization forbids unrelated options: "
                    + ", ".join(incompatible)
                )
            if args.source_ref is None:
                raise ValueError("--finalize-component requires --source-ref REF")
            if not args.replacement:
                raise ValueError(
                    "--finalize-component requires at least one --replacement"
                )
            manifest = load_manifest(args.manifest or DEFAULT_MANIFEST)
            finalized = finalize_component_replacement(
                manifest,
                args.repo,
                component_id=args.finalize_component,
                source_ref=args.source_ref,
                replacements=[
                    _parse_replacement(replacement)
                    for replacement in args.replacement
                ],
            )
            print(json.dumps(finalized, indent=2, sort_keys=True))
            return 0

        if args.release_candidate:
            incompatible = []
            if args.manifest is not None:
                incompatible.append("--manifest")
            if args.offline:
                incompatible.append("--offline")
            if args.installed_repo is not None:
                incompatible.append("--installed-repo")
            if incompatible:
                raise ValueError(
                    "strict release mode forbids override options: "
                    + ", ".join(incompatible)
                )
            report = audit_release_candidate(args.repo, args.release_candidate)
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                _print_human(report)
            return 0 if report["ready"] else 1

        if args.migrate_schema_1:
            legacy = load_manifest(args.migrate_schema_1)
            draft = migrate_schema_1(legacy)
            print(json.dumps(draft, indent=2, sort_keys=True))
            return 0

        manifest = load_manifest(args.manifest or DEFAULT_MANIFEST)
        if args.prepare_worktree:
            if args.offline:
                raise ValueError(
                    "--prepare-worktree requires live source-ref proof and forbids --offline"
                )
            receipt = prepare_worktree(
                manifest,
                args.repo,
                args.prepare_worktree,
                dry_run=args.dry_run,
            )
            print(json.dumps(receipt, indent=2, sort_keys=True))
            return 0
        report = audit_manifest(
            manifest,
            args.repo,
            installed_repository=args.installed_repo,
            observe_live=not args.offline,
        )
    except PreparationBlocked as exc:
        report = exc.to_dict()
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            for finding in report["findings"]:
                print(
                    f"error: {finding['code']}: {finding['message']}",
                    file=sys.stderr,
                )
        return 1
    except PreparationFailed as exc:
        report = {**exc.receipt, "error": str(exc)}
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except PublicationBlocked as exc:
        report = exc.report
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            for finding in report["findings"]:
                print(
                    f"error: {finding['code']}: {finding['message']}",
                    file=sys.stderr,
                )
        return 1
    except PublicationFailed as exc:
        report = {**exc.receipt, "error": str(exc)}
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except ReplacementFinalizationBlocked as exc:
        report = {
            "ready": False,
            "findings": [finding.to_dict() for finding in exc.findings],
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if args.json or args.finalize_component:
            print(json.dumps({"ready": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report)
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
