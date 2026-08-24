@AGENTS.md

# Hermes Agent

This checkout tracks **fork-integration** — current upstream `main` plus our
PRs. The agent guide for this repo is **[AGENTS.md](AGENTS.md)**; read it before
working here.

Its **"The fork-integration line"** section is the contract for what may land on
this branch: the same commits we propose upstream, no un-PR'd accretion,
converge the fork *to* the PR (never the reverse — a heavier local version that
"already works" is drift, not a superset), and prove any live-path change (the
updater) with two clean end-to-end runs from an isolated worktree, with a
`known-good` rollback ref pushed, before cutover.
