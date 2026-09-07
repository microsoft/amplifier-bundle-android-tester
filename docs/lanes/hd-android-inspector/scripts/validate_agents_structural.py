"""Run the DETERMINISTIC phases of foundation's `validate-agents` recipe.

`validate-agents` is a recipe, not a binary: four deterministic bash/python
steps (environment-check -> agent-discovery -> structural-validation ->
quality-classification) followed by four LLM steps. This lane's budget is $0
with no API calls, so only the four deterministic steps run -- and they are the
ones that decide the verdict this lane cares about: structural errors, the
`<example>`/`<commentary>` ban, and the description length gate.

The step bodies are read out of the installed recipe YAML and executed
verbatim, rather than copied here, so this script cannot drift from the
validator it claims to be running. The only substitutions are the recipe's own:
`{{repo_path}}` and the JSON hand-off between steps.

Usage:
    python3 validate_agents_structural.py <repo-path> [--label LABEL]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

DETERMINISTIC_STEPS = (
    "environment-check",
    "agent-discovery",
    "structural-validation",
    "quality-classification",
)


def find_recipe() -> Path:
    pattern = os.path.expanduser(
        "~/.amplifier/cache/skills/amplifier-foundation-*/recipes/validate-agents.yaml"
    )
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise SystemExit(f"validate-agents.yaml not found under {pattern}")
    return Path(matches[-1])


def run(repo_path: Path, label: str) -> int:
    import yaml

    recipe = find_recipe()
    steps = {
        s["id"]: s
        for s in yaml.safe_load(recipe.read_text())["steps"]
        if s.get("type") == "bash"
    }
    outputs: dict[str, str] = {}

    print(f"########## {label} ##########")
    print(f"repo: {repo_path}")
    print(f"recipe: {recipe} (v{_version(recipe)})")

    for step_id in DETERMINISTIC_STEPS:
        command = steps[step_id]["command"].replace("{{repo_path}}", str(repo_path))
        for name, value in outputs.items():
            command = command.replace("{{" + name + "}}", value)
        result = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True, cwd=repo_path
        )
        if result.returncode != 0:
            print(f"STEP {step_id} FAILED (exit {result.returncode})")
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            return result.returncode
        outputs[steps[step_id]["output"]] = result.stdout.strip()

    discovery = json.loads(outputs["discovery_results"])
    structural = json.loads(outputs["structural_results"])
    quality = json.loads(outputs["quality_classification"])

    print(f"agents discovered: {len(discovery.get('agents_found', []))}")
    print(f"structural summary: {structural.get('summary')}")
    print(f"quality_level: {quality.get('quality_level')}")
    print(f"quality summary: {quality.get('summary')}")
    for agent in structural.get("agents", []):
        codes = [w.get("code") for w in agent.get("warnings", [])]
        print(
            f"  {agent['name']:24s} chars={agent.get('description_length', 0):5d} "
            f"examples={agent.get('example_count', 0)} "
            f"commentary={agent.get('commentary_count', 0)} "
            f"errors={[e.get('code') for e in agent.get('errors', [])]} "
            f"warnings={codes}"
        )
    errors = structural.get("summary", {}).get("errors", -1)
    print(f"VERDICT: structural errors={errors} -> {'PASS' if errors == 0 else 'FAIL'}")
    return 0 if errors == 0 else 1


def _version(recipe: Path) -> str:
    import yaml

    return str(yaml.safe_load(recipe.read_text()).get("version", "?"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("repo_path")
    ap.add_argument("--label", default="REPO")
    args = ap.parse_args()
    raise SystemExit(run(Path(args.repo_path).resolve(), args.label))


if __name__ == "__main__":
    main()
