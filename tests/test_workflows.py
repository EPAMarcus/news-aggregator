"""The daily workflow's contract (architecture 3.1 and 4), checked on the YAML itself."""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


@pytest.fixture(scope="module")
def daily():
    data = yaml.safe_load((WORKFLOWS / "daily.yml").read_text())
    data["on"] = data.pop(True)  # YAML 1.1 reads the key `on` as a boolean
    return data


def run_step(workflow):
    (step,) = [s for s in workflow["jobs"]["run"]["steps"] if s.get("name") == "Run"]
    return step


def test_one_utc_cron_entry_at_06(daily):
    assert daily["on"]["schedule"] == [{"cron": "0 6 * * *"}]


def test_dispatch_inputs(daily):
    inputs = daily["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"level", "date", "force", "dry_run"}
    assert inputs["level"]["options"] == ["daily", "weekly", "monthly", "yearly"]
    assert inputs["dry_run"]["default"] is True  # a manual run is a dry run unless unticked


def test_permissions_and_shared_concurrency_group(daily):
    assert daily["permissions"] == {"contents": "write", "issues": "write"}
    assert daily["concurrency"] == {"group": "digests", "cancel-in-progress": False}


def test_only_the_anthropic_key_is_a_secret(daily):
    text = (WORKFLOWS / "daily.yml").read_text()
    assert set(re.findall(r"secrets\.(\w+)", text)) == {"ANTHROPIC_API_KEY"}
    env = run_step(daily)["env"]
    assert env["GH_TOKEN"] == "${{ github.token }}"


def test_inputs_are_passed_as_env_not_interpolated_into_the_script(daily):
    step = run_step(daily)
    assert "${{" not in step["run"]
    assert {"LEVEL", "DATE", "FORCE", "DRY_RUN"} <= set(step["env"])
    assert "uv run --no-sync aggregator" in step["run"]


def test_full_history_for_the_rebase(daily):
    checkout = daily["jobs"]["run"]["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 0
