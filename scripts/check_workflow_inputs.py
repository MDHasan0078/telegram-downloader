#!/usr/bin/env python3
"""Check the workflow's action steps for the mistakes that break it silently.

Two classes, and they are NOT equally fatal:

  1. A RESERVED STEP KEY nested inside a step's `with:`. `env`, `if`, `id`,
     `name`, `run`, `uses`, `with`, `continue-on-error`, `timeout-minutes`
     and `working-directory` are keys of the STEP. Inside `with:` they are
     not action inputs, and GitHub's schema validator rejects the WHOLE
     workflow for them:

         Invalid workflow file: .github/workflows/build.yml#L1
         (Line: 559, Col: 13): A mapping was not expected

     The run then fails with ZERO jobs, and the only place the reason
     appears is the web UI. This is not hypothetical: an `env:` block
     nested under `with:` for the android-emulator-runner action took the
     whole build pipeline down with it.

  2. An action input the action does not declare. GitHub only WARNS about
     these -- the workflow still runs, and the action ignores the key --
     so they are reported, not failed on.

    scripts/check_workflow_inputs.py [--workflow .github/workflows/build.yml]

Needs network access, so it runs in CI rather than in the test suite.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import urllib.error
import urllib.request

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_WORKFLOW = REPO_ROOT / ".github/workflows/build.yml"

# Keys of a STEP. None of them is an action input, and GitHub's schema
# validator treats them as a fatal error when they appear inside `with:`.
RESERVED_STEP_KEYS = {
    "env", "if", "id", "name", "run", "uses", "with",
    "continue-on-error", "timeout-minutes", "working-directory",
}


def _action_inputs(owner: str, repo_path: str, ref: str) -> set[str] | None:
    """The inputs an action declares, or None if it could not be read.

    `repo_path` is everything between the owner and the ref, e.g.
    "actions/checkout" or "gradle/actions/setup-gradle" -- the last of
    which is a subdirectory of the `gradle/actions` repository.
    """
    parts = [owner] + repo_path.split("/")
    repo = parts[1]
    subdir = "/".join(parts[2:])
    base = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}"
    if subdir:
        base = f"{base}/{subdir}"
    for filename in ("action.yml", "action.yaml"):
        try:
            with urllib.request.urlopen(f"{base}/{filename}", timeout=30) as resp:
                text = resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            continue
        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            continue
        inputs = doc.get("inputs")
        return set(inputs) if isinstance(inputs, dict) else set()
    return None


def _steps(workflow: dict):
    for job_name, job in (workflow.get("jobs") or {}).items():
        for index, step in enumerate(job.get("steps") or []):
            if isinstance(step, dict):
                yield job_name, index, step


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workflow", default=str(DEFAULT_WORKFLOW))
    args = ap.parse_args(argv)

    path = pathlib.Path(args.workflow)
    if not path.is_file():
        print(f"check_workflow_inputs: {path} does not exist", file=sys.stderr)
        return 1

    workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fatal: list[str] = []
    warnings: list[str] = []
    checked = 0

    for job_name, index, step in _steps(workflow):
        uses = step.get("uses")
        with_ = step.get("with")
        if not uses or not isinstance(with_, dict):
            continue

        # (1) a MAPPING-valued step key inside `with:` -- fatal.
        #
        # `env` is the one that bites. It is a key of the STEP whose value
        # is a mapping, and inside `with:` GitHub validates it as an action
        # input -- a string -- so it answers "A mapping was not expected"
        # and rejects the whole workflow. `name` is NOT in this set: it is
        # a real input of upload-artifact and download-artifact (the
        # artifact's name), and mistaking it for the step's name would
        # make this checker reject a workflow that works.
        mapping_keys = sorted(
            k for k, v in with_.items()
            if k in RESERVED_STEP_KEYS and isinstance(v, dict)
        )
        if mapping_keys:
            fatal.append(
                f"{path.name}: job {job_name!r} step {index}: {mapping_keys} "
                f"inside `with:`. That is a key of the STEP, and its value is "
                f"a mapping -- GitHub validates it as an action input and "
                f"rejects the WHOLE workflow: the run then fails with zero "
                f"jobs, and the reason appears only in the web UI.")

        if uses.startswith("./") or uses.startswith("docker://"):
            continue                       # a local or docker action
        m = re.match(r"^([^/@]+)/(.+)@([^@]+)$", uses)
        if not m:
            warnings.append(
                f"{path.name}: job {job_name!r} step {index}: cannot parse "
                f"uses {uses!r}")
            continue
        owner, repo_path, ref = m.group(1), m.group(2), m.group(3)
        declared = _action_inputs(owner, repo_path, ref)
        if declared is None:
            warnings.append(
                f"{path.name}: could not read the action.yml of {owner}/{repo_path} "
                f"at {ref[:12]} -- not checked")
            continue
        checked += 1
        # (2) an input the action does not declare -- a warning only.
        unknown = sorted(k for k in with_ if k not in declared)
        if unknown:
            warnings.append(
                f"{path.name}: job {job_name!r} step {index} ({repo_path}): "
                f"input(s) {unknown} are not declared by the action. GitHub "
                f"only warns about these and the action ignores them, but "
                f"it is usually a typo.")

    for problem in fatal:
        print(f"::error::{problem}", file=sys.stderr)
    for problem in warnings:
        print(f"::warning::{problem}", file=sys.stderr)
    if fatal:
        return 1
    print(f"  checked {checked} action step(s): no reserved step key is "
          f"nested inside a `with:`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
