"""Level 3 fixtures: a synthetic Borg repository built with the real borg binary (skipped when absent)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD = REPO_ROOT / "dev" / "synth" / "build_repo.py"


def borg_binary() -> str | None:
    for candidate in (
        os.environ.get("BORG"),
        str(REPO_ROOT / "dev" / "local" / "bin" / "borg"),
        shutil.which("borg"),
    ):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


@pytest.fixture(scope="session")
def borg() -> str:
    binary = borg_binary()
    if not binary:
        pytest.skip("no borg binary (run dev/get-borg.sh)")
    return binary


def _build(env: dict, repo: Path, scenario: str, generations: int) -> dict:
    out = subprocess.run(
        [sys.executable, str(BUILD), str(repo), "--scenario", scenario, "--generations", str(generations), "--start", "2026-09-10T02:00:00"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"synthetic repo build failed ({scenario}, rc={out.returncode}):\n{out.stdout[-1500:]}\n{out.stderr[-2500:]}")
    info = json.loads(out.stdout.strip().splitlines()[-1])
    info["borg"] = env["BORG"]
    info["base_dir"] = env["BORG_BASE_DIR"]
    return info


@pytest.fixture(scope="session")
def synthetic_repo(borg, tmp_path_factory) -> dict:
    """Normal scenario, 3 generations."""
    repo = tmp_path_factory.mktemp("repo") / "normal"
    env = dict(os.environ, BORG=borg, BORG_BASE_DIR=str(tmp_path_factory.mktemp("borgbase")))
    return _build(env, repo, "normal", 3)


def build_scenario(borg: str, tmp_path: Path, scenario: str, generations: int = 2) -> dict:
    repo = tmp_path / scenario
    env = dict(os.environ, BORG=borg, BORG_BASE_DIR=str(tmp_path / "base"))
    return _build(env, repo, scenario, generations)
