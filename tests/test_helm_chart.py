"""Validate the Helm chart at deploy/helm/."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parent.parent / "deploy" / "helm"

helm_available = pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not installed")


# --- Helm CLI tests (skipped if helm not installed) ---


@helm_available
def test_helm_lint():
    result = subprocess.run(["helm", "lint", str(CHART_DIR)], capture_output=True, text=True)
    assert result.returncode == 0, f"helm lint failed:\n{result.stderr}"


@helm_available
def test_helm_template():
    result = subprocess.run(
        ["helm", "template", "test-release", str(CHART_DIR)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"


# --- Structure validation (no helm required) ---


def test_chart_yaml_exists_and_valid():
    chart_file = CHART_DIR / "Chart.yaml"
    assert chart_file.exists()
    data = yaml.safe_load(chart_file.read_text())
    assert "name" in data
    assert "version" in data


def test_chart_yaml_required_fields():
    data = yaml.safe_load((CHART_DIR / "Chart.yaml").read_text())
    assert data["apiVersion"] == "v2"
    assert isinstance(data["name"], str) and data["name"]
    assert isinstance(data["version"], str) and data["version"]


def test_values_yaml_exists_and_valid():
    values_file = CHART_DIR / "values.yaml"
    assert values_file.exists()
    data = yaml.safe_load(values_file.read_text())
    assert isinstance(data, dict)


def test_values_yaml_expected_keys():
    data = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
    for key in ("image", "resources", "service", "nodeSelector", "tolerations"):
        assert key in data, f"Expected key '{key}' missing from values.yaml"


def test_templates_directory_non_empty():
    templates_dir = CHART_DIR / "templates"
    assert templates_dir.is_dir()
    files = list(templates_dir.iterdir())
    assert len(files) > 0, "templates/ directory is empty"
