"""Read-only Git audit for a schema-v2 fork integration manifest."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat as stat_module
import subprocess
from typing import Any, Callable, Sequence
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from .manifest import (
    Finding,
    findings_as_dicts,
    parse_manifest_json,
    validate_manifest,
)


UNKNOWN = "unknown"
CANONICAL_MANIFEST_PATH = "fork_integration/hermes-fork-manifest.v2.json"
DISABLED_HOOKS_PATH = f"{os.devnull}/fork-integration-disabled-hooks"
_SUBJECT_SCOPE_RE = re.compile(r"^(?:\[[^]]+\]\s*)?[a-zA-Z]+\(([^)]+)\):")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_ALLOWED_GIT_ENVIRONMENT_OVERRIDES = frozenset(
    {"GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"}
)


def sanitized_git_environment(
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return process environment without inherited Git routing/config state."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )
    if extra:
        environment.update(
            {
                key: value
                for key, value in extra.items()
                if not key.upper().startswith("GIT_")
                or key in _ALLOWED_GIT_ENVIRONMENT_OVERRIDES
            }
        )
    return environment


def _readonly_config_environment() -> dict[str, str]:
    """Allow direct config reads without inherited Git routing variables."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )
    return environment


def canonical_repository_identity(url: str, *, base: Path) -> str:
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.query or parsed.fragment:
        raise ValueError("repository URL must not contain a query or fragment")
    if parsed.scheme.casefold() == "file":
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("repository URL must not contain userinfo")
        authority = parsed.netloc
        decoded_path = unquote(parsed.path)
        if authority and authority.casefold() != "localhost":
            decoded_path = f"//{authority}{decoded_path}"
        value = url2pathname(decoded_path)
        parsed = urlsplit("")
    if parsed.scheme and parsed.netloc:
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("repository URL must not contain userinfo")
        host = parsed.hostname
        if not host:
            raise ValueError("repository URL does not contain a host")
        if ":" in host:
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("repository URL contains an invalid port") from exc
        default_ports = {"http": 80, "https": 443, "ssh": 22, "git": 9418}
        authority = host.casefold()
        if port is not None and port != default_ports.get(parsed.scheme.casefold()):
            authority += f":{port}"
        normalized_path = parsed.path.strip("/")
        if normalized_path.endswith(".git"):
            normalized_path = normalized_path[:-4]
        return f"{authority}/{normalized_path}"
    scp_match = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", value)
    if scp_match and not re.match(r"^[A-Za-z]:[\\/]", value):
        host, path = scp_match.groups()
        normalized_path = path.strip("/")
        if normalized_path.endswith(".git"):
            normalized_path = normalized_path[:-4]
        return f"{host.casefold()}/{normalized_path}"
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    normalized = path.resolve().as_posix().rstrip("/")
    return normalized.casefold() if os.name == "nt" else normalized


@dataclass(frozen=True)
class RepositoryResolution:
    """Canonical identity and local-remote binding for one declared URL."""

    identity: str | None
    is_bound: bool


class RepositoryBinding:
    """Compare declared repository URLs with configured remotes, fail closed."""

    def __init__(
        self,
        repository: str | Path,
        configured_remote_urls: Sequence[str],
    ) -> None:
        self.repository = Path(repository).resolve()
        self._configured_identities = frozenset(
            identity
            for url in configured_remote_urls
            if (identity := self._canonical_identity(url)) is not None
        )
        self._resolutions: dict[str, RepositoryResolution] = {}

    def _canonical_identity(self, url: object) -> str | None:
        if not isinstance(url, str) or not url.strip():
            return None
        try:
            return canonical_repository_identity(url, base=self.repository)
        except (OSError, ValueError):
            return None

    def resolve(self, url: object) -> RepositoryResolution:
        """Resolve a declaration once so identity and binding cannot diverge."""

        if not isinstance(url, str) or not url.strip():
            return RepositoryResolution(identity=None, is_bound=False)
        cached = self._resolutions.get(url)
        if cached is not None:
            return cached
        identity = self._canonical_identity(url)
        resolution = RepositoryResolution(
            identity=identity,
            is_bound=(
                identity is not None and identity in self._configured_identities
            ),
        )
        self._resolutions[url] = resolution
        return resolution

    def canonical_identity(self, url: object) -> str | None:
        return self.resolve(url).identity

    def is_bound(self, url: object) -> bool:
        return self.resolve(url).is_bound


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class GitProbe:
    """A small allow-listed facade around read-only Git commands."""

    def __init__(self, repository: str | Path, *, run: RunCommand = subprocess.run):
        self.repository = Path(repository).resolve()
        self._run_command = run
        self.commands: list[tuple[str, ...]] = []
        self.last_cleanliness_mode: str | None = None

    def _environment(self) -> dict[str, str]:
        return sanitized_git_environment()

    def _config_args(self, target: Path) -> tuple[str, ...]:
        return (
            "-c",
            f"safe.directory={target.as_posix()}",
            "-c",
            f"core.hooksPath={DISABLED_HOOKS_PATH}",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "tag.gpgSign=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.useReplaceRefs=false",
            "-c",
            "core.quotePath=true",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            f"core.excludesFile={os.devnull}",
            "-c",
            "submodule.recurse=false",
        )

    def _run(
        self,
        args: Sequence[str],
        *,
        repository: str | Path | None = None,
        input_text: str | None = None,
    ) -> GitResult:
        target = Path(repository).resolve() if repository is not None else self.repository
        command = (
            "git",
            *self._config_args(target),
            "-C",
            str(target),
            *args,
        )
        self.commands.append(command)
        try:
            completed = self._run_command(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                input=input_text,
                env=self._environment(),
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return GitResult(124, "", "git command timed out")
        except OSError:
            return GitResult(127, "", "git command could not be started")
        return GitResult(completed.returncode, completed.stdout, completed.stderr)

    def _run_global(self, args: Sequence[str]) -> GitResult:
        command = ("git", *self._config_args(self.repository), *args)
        self.commands.append(command)
        try:
            completed = self._run_command(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                cwd=self.repository,
                env=self._environment(),
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return GitResult(124, "", "git command timed out")
        except OSError:
            return GitResult(127, "", "git command could not be started")
        return GitResult(completed.returncode, completed.stdout, completed.stderr)

    def resolve_commit(
        self, revision: str, *, repository: str | Path | None = None
    ) -> str | None:
        if revision != "HEAD" and not _FULL_SHA_RE.fullmatch(
            revision.removesuffix("^")
        ):
            if not revision.startswith("refs/") or not self.check_ref_format(
                revision, repository=repository
            ):
                return None
        result = self._run(
            (
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{revision}^{{commit}}",
            ),
            repository=repository,
        )
        commit = result.stdout.strip()
        return commit if result.returncode == 0 and _FULL_SHA_RE.fullmatch(commit) else None

    def check_ref_format(
        self, ref: str, *, repository: str | Path | None = None
    ) -> bool:
        if not isinstance(ref, str) or not ref.startswith("refs/"):
            return False
        result = self._run(("check-ref-format", ref), repository=repository)
        return result.returncode == 0

    def check_branch_format(self, branch: str) -> bool:
        if not branch or branch.startswith("-"):
            return False
        return self._run(("check-ref-format", "--branch", branch)).returncode == 0

    def symbolic_ref(self, *, repository: str | Path | None = None) -> str | None:
        result = self._run(("symbolic-ref", "--quiet", "HEAD"), repository=repository)
        ref = result.stdout.strip()
        return ref if result.returncode == 0 and ref else None

    def remote_url(
        self, remote: str = "origin", *, repository: str | Path | None = None
    ) -> str | None:
        if not _REMOTE_NAME_RE.fullmatch(remote) or remote.startswith("-"):
            return None
        result = self._run(("remote", "get-url", remote), repository=repository)
        url = result.stdout.strip()
        return url if result.returncode == 0 and url else None

    def remote_urls(self) -> list[str]:
        result = self._run(("remote",))
        if result.returncode != 0:
            return []
        urls: list[str] = []
        for remote in result.stdout.splitlines():
            remote = remote.strip()
            if not _REMOTE_NAME_RE.fullmatch(remote) or remote.startswith("-"):
                continue
            remote_result = self._run(("remote", "get-url", "--all", remote))
            if remote_result.returncode == 0:
                urls.extend(line.strip() for line in remote_result.stdout.splitlines() if line.strip())
        return urls

    def repository_is_clean(self) -> bool | None:
        """Compare HEAD, index, and raw worktree bytes without content filters.

        ``git status`` and worktree ``git diff`` may execute a repository-local
        clean/process filter merely to refresh stat information.  Strict audit
        instead uses only ``ls-tree`` and ``ls-files`` plumbing, then hashes
        worktree bytes itself with Git's blob framing.  A text file may differ
        from its index blob only by CRLF-to-LF presentation, matching the
        native Git-for-Windows checkout contract without consulting attributes.
        Gitlinks fail closed rather than entering a submodule repository.
        """

        tree_result = self._run(("ls-tree", "-r", "-z", "--full-tree", "HEAD"))
        index_result = self._run(("ls-files", "--stage", "--cached", "-z"))
        untracked_result = self._run(
            ("ls-files", "--others", "--exclude-standard", "-z")
        )
        results = (tree_result, index_result, untracked_result)
        if any(result.returncode != 0 for result in results):
            self.last_cleanliness_mode = None
            return None
        if untracked_result.stdout:
            self.last_cleanliness_mode = None
            return False

        tree_entries = self._parse_tree_entries(tree_result.stdout)
        index_entries = self._parse_index_entries(index_result.stdout)
        if tree_entries is None or index_entries is None:
            self.last_cleanliness_mode = None
            return None
        if tree_entries != index_entries:
            self.last_cleanliness_mode = None
            return False

        crlf_paths: list[str] = []
        try:
            for path, (mode, object_id) in index_entries.items():
                if mode == "160000":
                    self.last_cleanliness_mode = None
                    return None
                worktree_path = self._worktree_path(path)
                if worktree_path is None:
                    self.last_cleanliness_mode = None
                    return None
                metadata = os.lstat(worktree_path)
                if stat_module.S_ISLNK(metadata.st_mode):
                    payload = os.readlink(worktree_path).encode(
                        "utf-8", errors="surrogateescape"
                    )
                elif stat_module.S_ISREG(metadata.st_mode):
                    payload = worktree_path.read_bytes()
                    if os.name != "nt" and mode in {"100644", "100755"}:
                        executable = bool(metadata.st_mode & 0o111)
                        if executable != (mode == "100755"):
                            self.last_cleanliness_mode = None
                            return False
                else:
                    self.last_cleanliness_mode = None
                    return False
                if self._blob_id(payload, object_id) == object_id:
                    continue
                if b"\0" in payload[:8000]:
                    self.last_cleanliness_mode = None
                    return False
                normalized = payload.replace(b"\r\n", b"\n")
                if normalized == payload or self._blob_id(normalized, object_id) != object_id:
                    self.last_cleanliness_mode = None
                    return False
                crlf_paths.append(path)
        except (OSError, UnicodeError):
            self.last_cleanliness_mode = None
            return None

        if crlf_paths:
            attributes = self._cached_checkout_attributes(crlf_paths)
            checkout_policy = self._checkout_eol_policy()
            if attributes is None or checkout_policy is None:
                self.last_cleanliness_mode = None
                return None
            if any(
                not self._crlf_checkout_allowed(attributes[path], checkout_policy)
                for path in crlf_paths
            ):
                self.last_cleanliness_mode = None
                return False

        self.last_cleanliness_mode = (
            "autocrlf" if crlf_paths else "raw-bytes"
        )
        return True

    def _cached_checkout_attributes(
        self, paths: Sequence[str]
    ) -> dict[str, tuple[str, str]] | None:
        result = self._run(
            ("check-attr", "--cached", "-z", "--stdin", "text", "eol"),
            input_text="\0".join(paths) + "\0",
        )
        if result.returncode != 0:
            return None
        values: dict[str, dict[str, str]] = {}
        records = result.stdout.split("\0")
        if records and records[-1] == "":
            records.pop()
        if len(records) % 3:
            return None
        for offset in range(0, len(records), 3):
            path, attribute, value = records[offset : offset + 3]
            if path not in paths or attribute not in {"text", "eol"}:
                return None
            path_values = values.setdefault(path, {})
            if attribute in path_values:
                return None
            path_values[attribute] = value
        if any(set(values.get(path, {})) != {"text", "eol"} for path in paths):
            return None
        return {
            path: (values[path]["text"], values[path]["eol"])
            for path in paths
        }

    def _checkout_eol_policy(self) -> tuple[str | None, str | None] | None:
        values: list[str | None] = []
        for key in ("core.autocrlf", "core.eol"):
            command = (
                "git",
                "--no-pager",
                "-C",
                str(self.repository),
                "config",
                "--no-includes",
                "--get",
                key,
            )
            self.commands.append(command)
            try:
                completed = self._run_command(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="surrogateescape",
                    env=_readonly_config_environment(),
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if completed.returncode == 1:
                values.append(None)
            elif completed.returncode == 0:
                value = completed.stdout.strip().casefold()
                values.append(value if value else None)
            else:
                return None
        return values[0], values[1]

    @staticmethod
    def _crlf_checkout_allowed(
        attributes: tuple[str, str],
        policy: tuple[str | None, str | None],
    ) -> bool:
        text_attribute, eol_attribute = attributes
        autocrlf, core_eol = policy
        if text_attribute == "unset" or eol_attribute == "lf":
            return False
        if eol_attribute == "crlf":
            return True
        if autocrlf in {"true", "yes", "on", "1"}:
            return True
        if autocrlf not in {None, "false", "no", "off", "0"}:
            return False
        if text_attribute not in {"set", "auto"}:
            return False
        if core_eol == "crlf":
            return True
        return (core_eol in {None, "native"}) and os.name == "nt"

    @staticmethod
    def _parse_tree_entries(output: str) -> dict[str, tuple[str, str]] | None:
        entries: dict[str, tuple[str, str]] = {}
        for record in output.split("\0"):
            if not record:
                continue
            header, separator, path = record.partition("\t")
            fields = header.split()
            if (
                not separator
                or len(fields) != 3
                or fields[1] not in {"blob", "commit"}
                or fields[0] not in {"100644", "100755", "120000", "160000"}
                or not _FULL_SHA_RE.fullmatch(fields[2])
                or path in entries
            ):
                return None
            entries[path] = (fields[0], fields[2])
        return entries

    @staticmethod
    def _parse_index_entries(output: str) -> dict[str, tuple[str, str]] | None:
        entries: dict[str, tuple[str, str]] = {}
        for record in output.split("\0"):
            if not record:
                continue
            header, separator, path = record.partition("\t")
            fields = header.split()
            if (
                not separator
                or len(fields) != 3
                or fields[2] != "0"
                or fields[0] not in {"100644", "100755", "120000", "160000"}
                or not _FULL_SHA_RE.fullmatch(fields[1])
                or path in entries
            ):
                return None
            entries[path] = (fields[0], fields[1])
        return entries

    def _worktree_path(self, path: str) -> Path | None:
        components = path.split("/")
        if (
            not path
            or path.startswith("/")
            or any(component in {"", ".", ".."} for component in components)
            or (os.name == "nt" and "\\" in path)
        ):
            return None
        return self.repository.joinpath(*components)

    @staticmethod
    def _blob_id(payload: bytes, expected: str) -> str | None:
        if len(expected) == 40:
            digest = hashlib.sha1()
        elif len(expected) == 64:
            digest = hashlib.sha256()
        else:
            return None
        digest.update(f"blob {len(payload)}\0".encode("ascii"))
        digest.update(payload)
        return digest.hexdigest()

    def read_blob(self, commit: str, path: str) -> str | None:
        if not _FULL_SHA_RE.fullmatch(commit) or path != CANONICAL_MANIFEST_PATH:
            return None
        result = self._run(("cat-file", "blob", f"{commit}:{path}"))
        return result.stdout if result.returncode == 0 else None

    def blob_id(self, commit: str, path: str) -> str | None:
        if not _FULL_SHA_RE.fullmatch(commit) or path != CANONICAL_MANIFEST_PATH:
            return None
        result = self._run(
            (
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{commit}:{path}",
            )
        )
        object_id = result.stdout.strip()
        return object_id if result.returncode == 0 and _FULL_SHA_RE.fullmatch(object_id) else None

    def subject(self, commit: str) -> str | None:
        if not _FULL_SHA_RE.fullmatch(commit):
            return None
        result = self._run(("show", "-s", "--format=%s", commit))
        return result.stdout.rstrip("\r\n") if result.returncode == 0 else None

    def commit_parents(self, commit: str) -> list[str] | None:
        if not _FULL_SHA_RE.fullmatch(commit):
            return None
        result = self._run(("rev-list", "--parents", "-n", "1", commit))
        fields = result.stdout.strip().split()
        if (
            result.returncode != 0
            or not fields
            or fields[0] != commit
            or not all(_FULL_SHA_RE.fullmatch(field) for field in fields)
        ):
            return None
        return fields[1:]

    def stable_patch_id(self, commit: str) -> str | None:
        if not _FULL_SHA_RE.fullmatch(commit):
            return None
        patch = self._run(
            (
                "show",
                "--format=email",
                "--patch",
                "--no-ext-diff",
                "--no-textconv",
                "--full-index",
                "--binary",
                "--no-color",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "--no-renames",
                "--diff-algorithm=myers",
                "--no-indent-heuristic",
                "--no-relative",
                "--ignore-submodules=none",
                "--submodule=short",
                "--unified=3",
                f"-O{os.devnull}",
                commit,
            )
        )
        if patch.returncode != 0:
            return None
        result = self._run(("patch-id", "--stable"), input_text=patch.stdout)
        first = result.stdout.split(maxsplit=1)[0] if result.stdout.strip() else ""
        return first if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", first) else None

    def changed_paths(self, commit: str) -> list[str] | None:
        if not _FULL_SHA_RE.fullmatch(commit):
            return None
        result = self._run(
            (
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "--no-renames",
                "-r",
                commit,
            )
        )
        if result.returncode != 0:
            return None
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def is_ancestor(self, ancestor: str, descendant: str) -> bool | None:
        if not _FULL_SHA_RE.fullmatch(ancestor) or not _FULL_SHA_RE.fullmatch(descendant):
            return None
        result = self._run(("merge-base", "--is-ancestor", ancestor, descendant))
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        return None

    def commits_between(self, base: str, head: str) -> list[str] | None:
        if not _FULL_SHA_RE.fullmatch(base) or not _FULL_SHA_RE.fullmatch(head):
            return None
        result = self._run(("rev-list", "--no-merges", f"{base}..{head}"))
        if result.returncode != 0:
            return None
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def commits_reachable(self, head: str) -> list[str] | None:
        if not _FULL_SHA_RE.fullmatch(head):
            return None
        result = self._run(("rev-list", "--no-merges", head))
        if result.returncode != 0:
            return None
        commits = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return commits if all(_FULL_SHA_RE.fullmatch(item) for item in commits) else None

    def linear_commits_between(self, base: str, head: str) -> list[str] | None:
        """Return the exact base-to-head chain, or None for merges/non-linearity."""

        if not _FULL_SHA_RE.fullmatch(base) or not _FULL_SHA_RE.fullmatch(head):
            return None
        result = self._run(
            ("rev-list", "--reverse", "--topo-order", "--parents", f"{base}..{head}")
        )
        if result.returncode != 0:
            return None
        expected_parent = base
        commits: list[str] = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if (
                len(fields) != 2
                or not _FULL_SHA_RE.fullmatch(fields[0])
                or fields[1] != expected_parent
            ):
                return None
            commits.append(fields[0])
            expected_parent = fields[0]
        if expected_parent != head:
            return None
        return commits

    def replay_metadata(self, commit: str) -> tuple[str, str | None, str] | None:
        """Return author, encoding, and message fields preserved by cherry-pick."""

        if not _FULL_SHA_RE.fullmatch(commit):
            return None
        result = self._run(("cat-file", "commit", commit))
        if result.returncode != 0 or "\n\n" not in result.stdout:
            return None
        headers, message = result.stdout.split("\n\n", 1)
        author = None
        encoding = None
        for line in headers.splitlines():
            if line.startswith("author "):
                author = line.removeprefix("author ")
            elif line.startswith("encoding "):
                encoding = line.removeprefix("encoding ")
        if author is None:
            return None
        return author, encoding, message

    def patch_present(self, head: str, source_commit: str) -> bool | None:
        """Return whether a historical equivalent remains in the current tree."""

        if not _FULL_SHA_RE.fullmatch(head) or not _FULL_SHA_RE.fullmatch(source_commit):
            return None
        ancestry = self.is_ancestor(source_commit, head)
        historical_match = ancestry is True
        if not historical_match:
            parent = self.resolve_commit(f"{source_commit}^")
            if parent is not None:
                result = self._run(("cherry", head, source_commit, parent))
                lines = [
                    line.strip() for line in result.stdout.splitlines() if line.strip()
                ]
                historical_match = (
                    result.returncode == 0
                    and len(lines) == 1
                    and lines[0].startswith("-")
                )
            if not historical_match:
                source_patch_id = self.stable_patch_id(source_commit)
                reachable = self.commits_reachable(head)
                if source_patch_id is None or reachable is None:
                    return None
                historical_match = any(
                    self.stable_patch_id(commit) == source_patch_id
                    for commit in reachable
                )
        if not historical_match:
            return False

        paths = self.changed_paths(source_commit)
        if not paths:
            return None
        current_tree = self._run(
            (
                "diff",
                "--quiet",
                "--no-ext-diff",
                "--no-textconv",
                source_commit,
                head,
                "--",
                *paths,
            )
        )
        if current_tree.returncode == 0:
            return True
        if current_tree.returncode == 1:
            return False
        return None

    def committer_environment(self, commit: str) -> dict[str, str] | None:
        if not _FULL_SHA_RE.fullmatch(commit):
            return None
        result = self._run(("show", "-s", "--format=%cn%x00%ce%x00%cI", commit))
        if result.returncode != 0:
            return None
        fields = result.stdout.rstrip("\r\n").split("\x00")
        if len(fields) != 3 or not all(fields):
            return None
        return {
            "GIT_COMMITTER_NAME": fields[0],
            "GIT_COMMITTER_EMAIL": fields[1],
            "GIT_COMMITTER_DATE": fields[2],
        }

    def live_ref(self, repository_url: str | None, ref: str | None) -> dict[str, Any]:
        observation = {
            "repository": repository_url if isinstance(repository_url, str) else UNKNOWN,
            "ref": ref if isinstance(ref, str) else UNKNOWN,
            "commit": UNKNOWN,
            "state": UNKNOWN,
        }
        if (
            not isinstance(repository_url, str)
            or not repository_url
            or not isinstance(ref, str)
            or not ref.startswith("refs/heads/")
        ):
            observation["detail"] = "repository URL or branch ref is not declared"
            return observation
        if not self.check_ref_format(ref):
            observation["detail"] = "ref fails git check-ref-format"
            return observation
        result = self._run_global(
            ("ls-remote", "--exit-code", "--refs", "--", repository_url, ref)
        )
        matches = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] == ref and _FULL_SHA_RE.fullmatch(fields[0]):
                matches.append(fields[0])
        if result.returncode == 0 and len(matches) == 1:
            observation["commit"] = matches[0]
            observation["state"] = "known"
        else:
            observation["detail"] = result.stderr.strip() or "ref was not observed exactly once"
        return observation


def _local_observation(probe: GitProbe, ref: str | None) -> dict[str, Any]:
    declared_ref = ref if isinstance(ref, str) else None
    commit = probe.resolve_commit(declared_ref) if declared_ref else None
    return {
        "repository": str(probe.repository),
        "ref": declared_ref or UNKNOWN,
        "commit": commit or UNKNOWN,
        "state": "known" if commit else UNKNOWN,
    }


def _installed_observation(
    probe: GitProbe, installed_repository: str | Path | None
) -> dict[str, Any]:
    if installed_repository is None:
        return {
            "repository": UNKNOWN,
            "ref": UNKNOWN,
            "commit": UNKNOWN,
            "remote_url": UNKNOWN,
            "state": UNKNOWN,
            "detail": "installed repository path was not supplied",
        }
    path = Path(installed_repository).resolve()
    commit = probe.resolve_commit("HEAD", repository=path)
    ref = probe.symbolic_ref(repository=path)
    remote_url = probe.remote_url(repository=path)
    return {
        "repository": str(path),
        "ref": ref or UNKNOWN,
        "commit": commit or UNKNOWN,
        "remote_url": remote_url or UNKNOWN,
        "state": "known" if commit else UNKNOWN,
    }


def _unobserved_ref(
    repository_url: object, ref: object, detail: str
) -> dict[str, Any]:
    displayed_repository = UNKNOWN
    if isinstance(repository_url, str):
        parsed = urlsplit(repository_url)
        if (
            not parsed.query
            and not parsed.fragment
            and parsed.username is None
            and parsed.password is None
        ):
            displayed_repository = repository_url
    return {
        "repository": displayed_repository,
        "ref": ref if isinstance(ref, str) else UNKNOWN,
        "commit": UNKNOWN,
        "state": UNKNOWN,
        "detail": detail,
    }


def _add(
    findings: list[Finding],
    code: str,
    message: str,
    *,
    component: str | None = None,
    patch: int | None = None,
    severity: str = "error",
) -> None:
    finding = Finding(code, severity, message, component, patch)
    if finding not in findings:
        findings.append(finding)


def _path_domain(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    if normalized.startswith(("tests/", "tests-js/", "docs/", "website/docs/")):
        return None
    parts = normalized.split("/")
    if len(parts) >= 2 and parts[0] == "apps":
        return "/".join(parts[:2])
    return parts[0]


def _subject_scope(subject: str) -> str | None:
    match = _SUBJECT_SCOPE_RE.match(subject)
    return match.group(1) if match else None


def _audit_component_cohesion(
    findings: list[Finding],
    component: dict[str, Any],
    inspected: list[dict[str, Any]],
) -> None:
    component_id = component.get("id")
    implementation = [item for item in inspected if item["role"] == "implementation"]
    if len(implementation) <= 1:
        return
    by_commit = {item["commit"]: index for index, item in enumerate(implementation)}
    edges: dict[int, set[int]] = {index: set() for index in range(len(implementation))}
    for left_index, left in enumerate(implementation):
        related_to = left.get("related_to")
        if isinstance(related_to, str) and related_to in by_commit:
            right_index = by_commit[related_to]
            edges[left_index].add(right_index)
            edges[right_index].add(left_index)
        for right_index in range(left_index + 1, len(implementation)):
            right = implementation[right_index]
            domains_overlap = bool(left["domains"] & right["domains"])
            scopes_match = left["scope"] is not None and left["scope"] == right["scope"]
            if domains_overlap or scopes_match:
                edges[left_index].add(right_index)
                edges[right_index].add(left_index)
    visited = {0}
    frontier = [0]
    while frontier:
        current = frontier.pop()
        for neighbor in edges[current] - visited:
            visited.add(neighbor)
            frontier.append(neighbor)
    if len(visited) != len(implementation):
        disconnected = [implementation[index]["commit"] for index in range(len(implementation)) if index not in visited]
        _add(
            findings,
            "unrelated_patches_grouped",
            "component contains disconnected implementation patches; split them or declare an explicit related_to edge: "
            + ", ".join(disconnected),
            component=component_id,
        )


def component_cohesion_findings(
    component: dict[str, Any], probe: GitProbe
) -> list[Finding]:
    """Inspect one component's local source objects for unrelated patches."""

    findings: list[Finding] = []
    inspected: list[dict[str, Any]] = []
    patches = component.get("patches")
    if not isinstance(patches, list):
        return findings
    for patch in patches:
        if not isinstance(patch, dict):
            continue
        source_identity = (
            patch.get("source") if isinstance(patch.get("source"), dict) else {}
        )
        source_commit = source_identity.get("commit")
        if not isinstance(source_commit, str) or probe.resolve_commit(source_commit) != source_commit:
            continue
        paths = probe.changed_paths(source_commit) or []
        actual_subject = probe.subject(source_commit) or ""
        inspected.append(
            {
                "commit": source_commit,
                "role": patch.get("role"),
                "related_to": patch.get("related_to"),
                "domains": {
                    domain for path in paths if (domain := _path_domain(path))
                },
                "scope": _subject_scope(actual_subject),
            }
        )
    _audit_component_cohesion(findings, component, inspected)
    return findings


def audit_manifest(
    manifest: dict[str, Any],
    repository: str | Path,
    *,
    installed_repository: str | Path | None = None,
    observe_live: bool = True,
    probe: GitProbe | None = None,
    strict_release: bool = False,
    strict_published_commit: str | None = None,
    initial_findings: Sequence[Finding] = (),
) -> dict[str, Any]:
    """Audit the manifest using only local object reads and git ls-remote."""

    git = probe or GitProbe(repository)
    findings = list(initial_findings)
    for finding in validate_manifest(manifest):
        if finding not in findings:
            findings.append(finding)
    repositories = manifest.get("repositories") if isinstance(manifest.get("repositories"), dict) else {}
    integration = manifest.get("integration") if isinstance(manifest.get("integration"), dict) else {}
    integration_repository_name = integration.get("repository")
    upstream_repository_name = integration.get("upstream_repository")
    integration_repository = (
        repositories.get(integration_repository_name, {})
        if isinstance(integration_repository_name, str)
        else {}
    )
    upstream_repository = (
        repositories.get(upstream_repository_name, {})
        if isinstance(upstream_repository_name, str)
        else {}
    )
    integration_url = integration_repository.get("url") if isinstance(integration_repository, dict) else None
    upstream_url = upstream_repository.get("url") if isinstance(upstream_repository, dict) else None
    integration_ref = integration.get("ref")
    upstream_ref = integration.get("upstream_ref")
    manifest_ready = manifest.get("manifest_state") == "ready"
    repository_binding = RepositoryBinding(git.repository, git.remote_urls())
    integration_repository_resolution = repository_binding.resolve(integration_url)
    upstream_repository_resolution = repository_binding.resolve(upstream_url)
    integration_repository_bound = integration_repository_resolution.is_bound
    upstream_repository_bound = upstream_repository_resolution.is_bound
    integration_repository_identity = integration_repository_resolution.identity

    for label, ref in (
        ("integration.ref", integration_ref),
        ("integration.upstream_ref", upstream_ref),
    ):
        if isinstance(ref, str) and not git.check_ref_format(ref):
            _add(
                findings,
                "invalid_git_ref",
                f"{label} fails git check-ref-format",
                component="integration",
            )

    if strict_release:
        if not observe_live:
            _add(
                findings,
                "release_live_observation_required",
                "strict release audit cannot disable live ref observation",
            )
        if not manifest_ready:
            _add(
                findings,
                "release_manifest_not_ready",
                "strict release audit requires manifest_state=ready",
            )
        for label, resolution in (
            ("integration", integration_repository_resolution),
            ("upstream", upstream_repository_resolution),
        ):
            if not resolution.is_bound:
                _add(
                    findings,
                    "release_repository_unbound",
                    f"declared {label} repository is not bound to a configured local remote",
                )

    if observe_live:
        upstream_observation = (
            git.live_ref(upstream_url, upstream_ref)
            if upstream_repository_bound
            else _unobserved_ref(
                upstream_url,
                upstream_ref,
                "audit refused an unbound repository URL",
            )
        )
        published_observation = (
            git.live_ref(integration_url, integration_ref)
            if integration_repository_bound
            else _unobserved_ref(
                integration_url,
                integration_ref,
                "audit refused an unbound repository URL",
            )
        )
    else:
        upstream_observation = {
            "repository": upstream_url if isinstance(upstream_url, str) else UNKNOWN,
            "ref": upstream_ref if isinstance(upstream_ref, str) else UNKNOWN,
            "commit": UNKNOWN,
            "state": UNKNOWN,
            "detail": "live observation disabled",
        }
        published_observation = {
            "repository": integration_url if isinstance(integration_url, str) else UNKNOWN,
            "ref": integration_ref if isinstance(integration_ref, str) else UNKNOWN,
            "commit": UNKNOWN,
            "state": UNKNOWN,
            "detail": "live observation disabled",
        }
    local_observation = _local_observation(git, integration_ref)
    installed_observation = _installed_observation(git, installed_repository)

    if manifest.get("manifest_state") == "review_required":
        _add(findings, "manifest_review_required", "manifest is explicitly marked review_required")

    expected_base = integration.get("expected_base_commit")
    expected_head = integration.get("expected_head_commit")
    resolved_base = git.resolve_commit(expected_base) if isinstance(expected_base, str) else None
    resolved_head = git.resolve_commit(expected_head) if isinstance(expected_head, str) else None
    if expected_base is not None and resolved_base is None:
        _add(findings, "expected_base_missing", "expected integration base is not a local commit")
    if expected_head is not None and resolved_head is None:
        _add(findings, "expected_head_missing", "expected integration head is not a local commit")
    if expected_head is not None and local_observation["commit"] != UNKNOWN:
        if expected_head != local_observation["commit"]:
            _add(findings, "local_head_mismatch", "local integration ref does not equal expected_head_commit")
    if resolved_base is not None and resolved_head is not None:
        if git.is_ancestor(resolved_base, resolved_head) is not True:
            _add(
                findings,
                "integration_base_not_ancestor",
                "expected_base_commit is not an ancestor of expected_head_commit",
            )
    if strict_release:
        if upstream_observation.get("commit") != expected_base:
            _add(
                findings,
                "upstream_base_mismatch",
                "live upstream ref does not equal expected_base_commit",
            )
        published_expectation = (
            strict_published_commit
            if strict_published_commit is not None
            else expected_head
        )
        if published_observation.get("commit") != published_expectation:
            _add(
                findings,
                (
                    "published_expected_old_mismatch"
                    if strict_published_commit is not None
                    else "published_head_mismatch"
                ),
                (
                    "live published ref does not equal the explicit expected-old commit"
                    if strict_published_commit is not None
                    else "live published ref does not equal expected_head_commit"
                ),
            )

    local_integration_commit = (
        local_observation["commit"] if local_observation["commit"] != UNKNOWN else None
    )
    live_source_cache: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    inspected_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    patches_by_source_patch_id: dict[str, dict[str, Any]] = {}

    components = manifest.get("components") if isinstance(manifest.get("components"), list) else []
    for component in components:
        if not isinstance(component, dict):
            continue
        component_id = component.get("id") if isinstance(component.get("id"), str) else None
        status = component.get("upstream_status")
        component_patch_values = (
            component.get("patches")
            if isinstance(component.get("patches"), list)
            else []
        )
        has_required_patch = any(
            isinstance(item, dict) and item.get("disposition") == "required"
            for item in component_patch_values
        )
        source = component.get("source") if isinstance(component.get("source"), dict) else {}
        source_repository_name = source.get("repository")
        source_repository = (
            repositories.get(source_repository_name, {})
            if isinstance(source_repository_name, str)
            else {}
        )
        source_url = source_repository.get("url") if isinstance(source_repository, dict) else None
        source_ref = source.get("ref")
        source_ref_valid = isinstance(source_ref, str) and git.check_ref_format(source_ref)
        local_source_tip = git.resolve_commit(source_ref) if source_ref_valid else None
        source_repository_resolution = repository_binding.resolve(source_url)
        source_repository_bound = source_repository_resolution.is_bound
        source_repository_identity = source_repository_resolution.identity

        if isinstance(source_ref, str) and not source_ref_valid:
            _add(
                findings,
                "invalid_git_ref",
                "component source ref fails git check-ref-format",
                component=component_id,
            )
        if manifest_ready:
            if local_source_tip is None:
                _add(
                    findings,
                    "source_ref_unavailable",
                    "ready component source ref is not available locally",
                    component=component_id,
                )
            if not source_repository_bound:
                _add(
                    findings,
                    "source_repository_unbound",
                    "ready component source repository is not bound to a configured local remote",
                    component=component_id,
                )
        if (
            source_ref == integration_ref
            and source_repository_identity is not None
            and source_repository_identity == integration_repository_identity
        ):
            _add(
                findings,
                "source_target_same_ref",
                "component source and integration target are the same canonical repository ref",
                component=component_id,
            )

        if (
            local_source_tip is not None
            and local_integration_commit is not None
            and local_source_tip == local_integration_commit
        ):
            _add(
                findings,
                "circular_source_ref",
                "component source ref resolves to the integration branch tip",
                component=component_id,
            )
        if (
            has_required_patch
            and local_source_tip is not None
            and resolved_head is not None
            and git.is_ancestor(local_source_tip, resolved_head) is True
        ):
            _add(
                findings,
                "source_ref_integration_lineage",
                "required component source ref is reachable from the integration lineage",
                component=component_id,
            )
        live_source: dict[str, Any] | None = None
        if observe_live and isinstance(source_url, str) and source_ref_valid:
            cache_key = (source_url, source_ref)
            if not source_repository_bound:
                live_source = _unobserved_ref(
                    source_url,
                    source_ref,
                    "audit refused an unbound repository URL",
                )
            else:
                if cache_key not in live_source_cache:
                    live_source_cache[cache_key] = git.live_ref(
                        source_url, source_ref
                    )
                live_source = live_source_cache[cache_key]
            if manifest_ready:
                if live_source.get("commit") == UNKNOWN:
                    _add(
                        findings,
                        "source_ref_live_unknown",
                        "ready component source ref could not be proven live",
                        component=component_id,
                    )
                elif local_source_tip is not None and live_source.get("commit") != local_source_tip:
                    _add(
                        findings,
                        "source_ref_live_mismatch",
                        "live component source ref does not equal its local ref",
                        component=component_id,
                    )
            if (
                (
                    source_repository_name == integration_repository_name
                    or (
                        source_repository_identity is not None
                        and source_repository_identity
                        == integration_repository_identity
                    )
                )
                and live_source.get("commit") != UNKNOWN
                and live_source.get("commit") == published_observation.get("commit")
            ):
                _add(
                    findings,
                    "circular_source_ref",
                    "live component source resolves to the published integration tip",
                    component=component_id,
                )

        inspected_component: list[dict[str, Any]] = []
        patches = component_patch_values
        for patch_index, patch in enumerate(patches):
            if not isinstance(patch, dict):
                continue
            source_identity = patch.get("source") if isinstance(patch.get("source"), dict) else {}
            source_commit = source_identity.get("commit")
            declared_patch_id = source_identity.get("stable_patch_id")
            if isinstance(declared_patch_id, str):
                patches_by_source_patch_id.setdefault(declared_patch_id, patch)
            if not isinstance(source_commit, str):
                continue
            resolved_source = git.resolve_commit(source_commit)
            if resolved_source != source_commit:
                severity = "warning" if status == "review_required" else "error"
                _add(
                    findings,
                    "source_commit_unavailable",
                    "source commit is unavailable or does not resolve to its declared full SHA",
                    component=component_id,
                    patch=patch_index,
                    severity=severity,
                )
                continue
            if patch.get("disposition") == "absorbed_upstream":
                absorbed = (
                    git.patch_present(resolved_base, source_commit)
                    if resolved_base is not None
                    else None
                )
                if absorbed is not True:
                    _add(
                        findings,
                        "absorbed_patch_missing",
                        "absorbed_upstream patch is not represented in expected_base_commit",
                        component=component_id,
                        patch=patch_index,
                    )
            computed_patch_id = git.stable_patch_id(source_commit)
            if computed_patch_id != declared_patch_id:
                _add(
                    findings,
                    "source_patch_id_mismatch",
                    f"computed source stable patch id is {computed_patch_id or UNKNOWN}",
                    component=component_id,
                    patch=patch_index,
                )
            actual_subject = git.subject(source_commit)
            if actual_subject != patch.get("subject"):
                _add(
                    findings,
                    "source_subject_mismatch",
                    f"source subject is {actual_subject or UNKNOWN!r}",
                    component=component_id,
                    patch=patch_index,
                )
            if local_source_tip is not None:
                reachable = git.is_ancestor(source_commit, local_source_tip)
                if reachable is not True:
                    _add(
                        findings,
                        "source_commit_not_reachable",
                        "source commit is not reachable from the component source ref",
                        component=component_id,
                        patch=patch_index,
                    )
            paths = git.changed_paths(source_commit) or []
            inspected = {
                "commit": source_commit,
                "role": patch.get("role"),
                "related_to": patch.get("related_to"),
                "domains": {domain for path in paths if (domain := _path_domain(path))},
                "scope": _subject_scope(actual_subject or ""),
            }
            inspected_component.append(inspected)
            inspected_by_key[(component_id or "", patch_index)] = inspected

            final_identity = patch.get("integration") if isinstance(patch.get("integration"), dict) else {}
            final_commit = final_identity.get("commit")
            final_patch_id = final_identity.get("stable_patch_id")
            if patch.get("disposition") == "required":
                if source_commit == final_commit and isinstance(source_commit, str):
                    _add(
                        findings,
                        "source_final_commit_same",
                        "required source and integration commits must be distinct objects",
                        component=component_id,
                        patch=patch_index,
                    )
                if (
                    resolved_head is not None
                    and isinstance(source_commit, str)
                    and git.is_ancestor(source_commit, resolved_head) is True
                ):
                    _add(
                        findings,
                        "source_commit_integration_lineage",
                        "required source commit is reachable from the integration lineage",
                        component=component_id,
                        patch=patch_index,
                    )
            if isinstance(final_commit, str):
                resolved_final = git.resolve_commit(final_commit)
                if resolved_final != final_commit:
                    _add(findings, "integration_commit_unavailable", "integration commit is unavailable", component=component_id, patch=patch_index)
                else:
                    computed_final_patch_id = git.stable_patch_id(final_commit)
                    if computed_final_patch_id != final_patch_id:
                        _add(
                            findings,
                            "integration_patch_id_mismatch",
                            f"computed integration stable patch id is {computed_final_patch_id or UNKNOWN}",
                            component=component_id,
                            patch=patch_index,
                        )
                    if (
                        local_integration_commit is not None
                        and git.is_ancestor(final_commit, local_integration_commit)
                        is not True
                    ):
                        _add(findings, "integration_commit_not_reachable", "integration commit is not reachable from integration.ref", component=component_id, patch=patch_index)
        _audit_component_cohesion(findings, component, inspected_component)

    if strict_release and resolved_base is not None and resolved_head is not None:
        required_patch_ids = manifest.get("required_patch_ids")
        if not isinstance(required_patch_ids, list):
            required_patch_ids = []
        expected_history: list[str] = []
        history_contract_complete = True
        for required_patch_id in required_patch_ids:
            if not isinstance(required_patch_id, str):
                history_contract_complete = False
                continue
            patch = patches_by_source_patch_id.get(required_patch_id)
            if not isinstance(patch, dict):
                history_contract_complete = False
                continue
            source_identity = (
                patch.get("source") if isinstance(patch.get("source"), dict) else {}
            )
            final_identity = (
                patch.get("integration")
                if isinstance(patch.get("integration"), dict)
                else {}
            )
            source_commit = source_identity.get("commit")
            final_commit = final_identity.get("commit")
            if not (
                isinstance(source_commit, str)
                and _FULL_SHA_RE.fullmatch(source_commit)
                and isinstance(final_commit, str)
                and _FULL_SHA_RE.fullmatch(final_commit)
            ):
                history_contract_complete = False
                continue
            expected_history.append(final_commit)
            source_metadata = git.replay_metadata(source_commit)
            final_metadata = git.replay_metadata(final_commit)
            if source_metadata is None or final_metadata is None:
                _add(
                    findings,
                    "integration_replay_metadata_unavailable",
                    "strict release could not inspect cherry-pick-preserved metadata",
                )
            elif source_metadata != final_metadata:
                _add(
                    findings,
                    "integration_replay_metadata_mismatch",
                    "expected integration commit does not preserve source author and message metadata",
                )
        actual_history = git.linear_commits_between(resolved_base, resolved_head)
        if (
            not history_contract_complete
            or actual_history is None
            or actual_history != expected_history
        ):
            _add(
                findings,
                "integration_history_mismatch",
                "base-to-head history must be a linear chain of exactly the ordered required integration commits",
            )

    history: list[dict[str, str]] = []
    if isinstance(expected_base, str) and local_integration_commit is not None:
        history_commits = git.commits_between(expected_base, local_integration_commit)
        if history_commits is not None:
            for commit in history_commits:
                subject = git.subject(commit)
                patch_id = git.stable_patch_id(commit)
                if subject is not None and patch_id is not None:
                    history.append({"commit": commit, "subject": subject, "stable_patch_id": patch_id})
    for component in components:
        if not isinstance(component, dict):
            continue
        component_id = component.get("id") if isinstance(component.get("id"), str) else None
        component_patches = component.get("patches")
        if not isinstance(component_patches, list):
            component_patches = []
        for patch_index, patch in enumerate(component_patches):
            if not isinstance(patch, dict):
                continue
            final_identity = patch.get("integration") if isinstance(patch.get("integration"), dict) else {}
            if final_identity.get("state") == "not_replayed":
                continue
            subject_matches = [item for item in history if item["subject"] == patch.get("subject")]
            expected_patch_id = final_identity.get("stable_patch_id")
            if expected_patch_id is None and subject_matches:
                distinct = sorted({item["stable_patch_id"] for item in subject_matches})
                _add(
                    findings,
                    "same_subject_non_equivalent",
                    "integration history has subject-only candidates but no expected stable patch identity: "
                    + ", ".join(distinct),
                    component=component_id,
                    patch=patch_index,
                )
            elif expected_patch_id is not None and not any(
                item["stable_patch_id"] == expected_patch_id for item in history
            ):
                non_equivalent = sorted({item["stable_patch_id"] for item in subject_matches})
                detail = "; same-subject non-equivalent candidates: " + ", ".join(non_equivalent) if non_equivalent else ""
                _add(
                    findings,
                    "expected_patch_missing",
                    "expected integration patch identity is not present" + detail,
                    component=component_id,
                    patch=patch_index,
                )

    identity_findings = []
    for name, observation in (
        ("upstream", upstream_observation),
        ("published", published_observation),
        ("local", local_observation),
        ("installed", installed_observation),
    ):
        if observation["state"] == UNKNOWN:
            identity_findings.append(name)
    if identity_findings:
        _add(
            findings,
            "identity_unknown",
            "unavailable identities: " + ", ".join(identity_findings),
            severity="warning",
        )

    errors = [finding for finding in findings if finding.severity == "error"]
    return {
        "schema_version": manifest.get("schema_version", UNKNOWN),
        "manifest_state": manifest.get("manifest_state", UNKNOWN),
        "ready": not errors,
        "dry_run": True,
        "writes": [],
        "identities": {
            "upstream": upstream_observation,
            "published": published_observation,
            "local": local_observation,
            "installed": installed_observation,
        },
        "expectations": {
            "integration_base": expected_base or UNKNOWN,
            "integration_head": expected_head or UNKNOWN,
            "published": (
                strict_published_commit
                if strict_published_commit is not None
                else expected_head or UNKNOWN
            ),
        },
        "findings": findings_as_dicts(findings),
        "git_commands": [list(command) for command in git.commands],
    }


def _audit_release_candidate(
    repository: str | Path,
    candidate_commit: str,
    *,
    expected_published_commit: str | None = None,
) -> dict[str, Any]:
    """Strictly audit the canonical manifest blob committed at candidate_commit."""

    repository_path = Path(repository).resolve()
    setup_findings: list[Finding] = []
    git = GitProbe(repository_path)

    if expected_published_commit is not None and (
        not isinstance(expected_published_commit, str)
        or not _FULL_SHA_RE.fullmatch(expected_published_commit)
    ):
        _add(
            setup_findings,
            "invalid_expected_published_commit",
            "expected published commit must be exactly 40 lowercase hexadecimal characters",
        )

    resolved_candidate = None
    if not isinstance(candidate_commit, str) or not _FULL_SHA_RE.fullmatch(
        candidate_commit
    ):
        _add(
            setup_findings,
            "invalid_release_candidate",
            "release candidate must be exactly 40 lowercase hexadecimal characters",
        )
    else:
        resolved_candidate = git.resolve_commit(candidate_commit)
        if resolved_candidate != candidate_commit:
            _add(
                setup_findings,
                "release_candidate_unavailable",
                "release candidate is not an available local commit",
            )

    clean = git.repository_is_clean()
    if clean is not True:
        _add(
            setup_findings,
            "release_repository_dirty",
            "strict release audit requires a clean candidate checkout",
        )
    head = git.resolve_commit("HEAD")
    if resolved_candidate is not None and head != resolved_candidate:
        _add(
            setup_findings,
            "release_candidate_not_head",
            "release candidate must be the exact checked-out HEAD",
        )

    blob = (
        git.read_blob(resolved_candidate, CANONICAL_MANIFEST_PATH)
        if resolved_candidate is not None
        else None
    )
    blob_id = (
        git.blob_id(resolved_candidate, CANONICAL_MANIFEST_PATH)
        if resolved_candidate is not None
        else None
    )
    manifest: dict[str, Any] = {}
    if blob is None or blob_id is None:
        _add(
            setup_findings,
            "release_manifest_blob_missing",
            f"candidate does not contain {CANONICAL_MANIFEST_PATH}",
        )
    else:
        try:
            blob.encode("utf-8", errors="strict")
            manifest = parse_manifest_json(blob)
        except (UnicodeEncodeError, ValueError, json.JSONDecodeError) as exc:
            _add(
                setup_findings,
                "release_manifest_blob_invalid",
                f"candidate manifest blob is invalid: {exc}",
            )

    expected_head = (
        manifest.get("integration", {}).get("expected_head_commit")
        if isinstance(manifest.get("integration"), dict)
        else None
    )
    if (
        resolved_candidate is not None
        and isinstance(expected_head, str)
        and _FULL_SHA_RE.fullmatch(expected_head)
    ):
        if git.commit_parents(resolved_candidate) != [expected_head]:
            _add(
                setup_findings,
                "release_candidate_parent_mismatch",
                "release candidate must have expected_head_commit as its only parent",
            )
        changed_paths = git.changed_paths(resolved_candidate)
        if changed_paths != [CANONICAL_MANIFEST_PATH]:
            _add(
                setup_findings,
                "release_candidate_not_manifest_only",
                "release candidate may change only the canonical manifest path",
            )

    report = audit_manifest(
        manifest,
        repository_path,
        observe_live=True,
        probe=git,
        strict_release=True,
        strict_published_commit=expected_published_commit,
        initial_findings=setup_findings,
    )
    report["release_candidate"] = {
        "repository": str(repository_path),
        "commit": resolved_candidate or UNKNOWN,
        "checked_out_head": head or UNKNOWN,
        "manifest_path": CANONICAL_MANIFEST_PATH,
        "manifest_blob": blob_id or UNKNOWN,
        "clean": clean if clean is not None else UNKNOWN,
        "cleanliness_mode": git.last_cleanliness_mode or UNKNOWN,
        "expected_published_commit": (
            expected_published_commit
            if expected_published_commit is not None
            else report["expectations"]["integration_head"]
        ),
    }
    return report


def audit_release_candidate(
    repository: str | Path,
    candidate_commit: str,
) -> dict[str, Any]:
    """Audit an already-published candidate against its declared final head."""

    return _audit_release_candidate(repository, candidate_commit)


def audit_publication_candidate(
    repository: str | Path,
    candidate_commit: str,
    expected_old_commit: str,
) -> dict[str, Any]:
    """Preflight a candidate while pinning the currently published old SHA."""

    return _audit_release_candidate(
        repository,
        candidate_commit,
        expected_published_commit=expected_old_commit,
    )
