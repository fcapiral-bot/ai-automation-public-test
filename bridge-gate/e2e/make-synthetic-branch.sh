#!/usr/bin/env bash
# Creates ONE throwaway branch claude/e2e-synthetic-<suffix> from origin/main holding a deliberately buggy helper and its tests
# (the CI `unit-tests` check fails on it). It commits locally and prints the next steps; it pushes nothing and opens nothing.
# Usage (from a clone, with a clean tree): bridge-gate/e2e/make-synthetic-branch.sh a
set -euo pipefail
suffix=${1:?usage: make-synthetic-branch.sh <suffix, e.g. a>}
[[ $suffix =~ ^[a-z0-9-]{1,20}$ ]] || { echo "suffix must be 1-20 of a-z 0-9 -" >&2; exit 1; }
branch="claude/e2e-synthetic-$suffix"
here=$(cd "$(dirname "$0")" && pwd)
[[ -z $(git status --porcelain) ]] || { echo "working tree is not clean" >&2; exit 1; }
git fetch -q origin main
git switch -q -c "$branch" origin/main
mkdir -p synthetic
: > synthetic/__init__.py
cp "$here/synthetic/pricing.py.txt" synthetic/pricing.py
cp "$here/synthetic/test_pricing.py.txt" tests/test_synthetic_pricing.py
git add synthetic tests/test_synthetic_pricing.py
git commit -q -m "Add synthetic pricing helper (disposable bridge end-to-end test)"
echo "Created $branch (1 commit). Next: git push -u origin $branch, then open a NON-DRAFT pull request. Never merge it."
