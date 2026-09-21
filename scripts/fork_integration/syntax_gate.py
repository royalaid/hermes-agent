#!/usr/bin/env python3
"""Parse-check a composed fork-integration tree before it is published.

`refresh.py` replays the fork range onto fresh upstream with `rerere.autoupdate`
on, so recorded conflict resolutions are staged without review. It then proves
*provenance* (`_rerere_paths_accounted_for`) and a handful of `TreeAssertion`
substrings — never that the result still parses. A stale recorded resolution
replayed onto a drifted conflict produces exactly two shapes, and both reached
a published tip in Sep 2026: two lines welded together (the newline between
them lost) and a line emitted twice.

That published tip could not import `hermes_cli.main_desktop`, so every
`hermes` command died; `hermes_state.py` could not build `SessionDB`, so the
gateway would not start; and `apps/desktop/electron/main.ts` had two welded
imports, so `hermes desktop` could not bundle. None of it was caught before
publication because nothing in the pipeline ever parsed a file.

This gate is that missing step. Run from the tree being checked:

    python scripts/fork_integration/syntax_gate.py --esbuild-root <repo-with-node_modules>

`git ls-files` is read from the CURRENT directory (the scratch worktree), while
node's esbuild is resolved from `--esbuild-root`, because a `git worktree` has
no `node_modules` of its own.
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

# Parsing every tracked file is the point, but a vendored or generated tree can
# carry deliberately unparseable fixtures; keep the skip list explicit and tiny.
SKIP_PREFIXES = ("node_modules/",)


def tracked(cwd: Path, *patterns: str) -> list[str]:
    done = subprocess.run(
        ["git", "-C", str(cwd), "ls-files", *patterns],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    return [p for p in done.stdout.splitlines() if p and not p.startswith(SKIP_PREFIXES)]


def check_python(cwd: Path) -> list[str]:
    """ast.parse every tracked .py, plus the one duplication shape that parses.

    A duplicated line inside a class's base list is valid Python and only fails
    at import (`TypeError: duplicate base class`), which is how a broken
    SessionDB shipped. Catch it here rather than at gateway start.
    """
    failures: list[str] = []

    for rel in tracked(cwd, "*.py"):
        path = cwd / rel
        try:
            source = path.read_bytes()
        except OSError as error:
            failures.append(f"{rel}: unreadable: {error}")
            continue

        try:
            tree = ast.parse(source, filename=rel)
        except (SyntaxError, ValueError) as error:
            line = getattr(error, "lineno", None) or 0
            text = (getattr(error, "text", "") or "").strip()
            failures.append(f"{rel}:{line}: {error.msg if isinstance(error, SyntaxError) else error}"
                            + (f"  |  {text}" if text else ""))
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            names = [base.id for base in node.bases if isinstance(base, ast.Name)]
            for name, count in Counter(names).items():
                if count > 1:
                    failures.append(
                        f"{rel}:{node.lineno}: class {node.name} lists base {name} {count} times "
                        f"(TypeError at import)"
                    )

    return failures


ESBUILD_PROBE = r"""
import { readFileSync } from 'node:fs'
import path from 'node:path'
import * as esbuild from 'esbuild'

const [treeRoot, listFile] = process.argv.slice(2)
const files = JSON.parse(readFileSync(listFile, 'utf8'))
const loaderFor = f =>
  f.endsWith('.tsx') ? 'tsx' : f.endsWith('.ts') ? 'ts' : f.endsWith('.jsx') ? 'jsx' : 'js'

const failures = []
for (const rel of files) {
  let source
  try {
    source = readFileSync(path.join(treeRoot, rel), 'utf8')
  } catch (error) {
    failures.push(`${rel}: unreadable: ${error.message}`)
    continue
  }
  try {
    esbuild.transformSync(source, { loader: loaderFor(rel), sourcefile: rel })
  } catch (error) {
    for (const item of error.errors ?? [{ text: String(error) }]) {
      const loc = item.location
      failures.push(`${rel}:${loc ? `${loc.line}:${loc.column}` : '?'}: ${item.text}`)
    }
  }
}
process.stdout.write(JSON.stringify(failures))
"""


def check_js(cwd: Path, esbuild_root: Path) -> list[str]:
    """Parse every tracked TS/JS with the repo's own esbuild.

    esbuild also reports duplicate declarations in one scope, which is the
    JS face of the duplicated-line shape (`const portAnnouncement` twice, and
    with it a second `await claimBackendChild`).
    """
    files = tracked(cwd, *(f"*{suffix}" for suffix in JS_SUFFIXES))
    if not files:
        return []

    if not (esbuild_root / "node_modules" / "esbuild").is_dir():
        # Never degrade silently: an unchecked TS tree is how the desktop
        # bundle broke, so say so and fail.
        return [f"esbuild not found under {esbuild_root / 'node_modules'}; cannot parse-check "
                f"{len(files)} TS/JS files"]

    list_file = esbuild_root / ".syntax-gate-files.json"
    probe = esbuild_root / ".syntax-gate-probe.mjs"
    try:
        list_file.write_text(json.dumps(files), encoding="utf-8")
        probe.write_text(ESBUILD_PROBE, encoding="utf-8")
        done = subprocess.run(
            ["node", str(probe), str(cwd), str(list_file)],
            cwd=esbuild_root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=1800,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return [f"esbuild probe failed to run: {error}"]
    finally:
        for temp in (list_file, probe):
            temp.unlink(missing_ok=True)

    if done.returncode:
        return [f"esbuild probe exited {done.returncode}: {(done.stderr or done.stdout).strip()[:500]}"]

    try:
        return list(json.loads(done.stdout or "[]"))
    except json.JSONDecodeError:
        return [f"esbuild probe returned unparseable output: {done.stdout[:300]}"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", default=".", help="tree to check (default: cwd)")
    parser.add_argument("--esbuild-root", default=None,
                        help="directory holding node_modules/esbuild (default: --tree)")
    parser.add_argument("--python-only", action="store_true",
                        help="skip the TS/JS pass (use only where node is unavailable)")
    args = parser.parse_args(argv)

    tree = Path(args.tree).resolve()
    esbuild_root = Path(args.esbuild_root).resolve() if args.esbuild_root else tree

    failures = check_python(tree)
    if not args.python_only:
        failures += check_js(tree, esbuild_root)

    if failures:
        print(f"syntax gate FAILED: {len(failures)} problem(s) in {tree}", file=sys.stderr)
        for failure in failures[:50]:
            print(f"  {failure}", file=sys.stderr)
        if len(failures) > 50:
            print(f"  ... and {len(failures) - 50} more", file=sys.stderr)
        return 1

    print(f"syntax gate OK: {tree}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
