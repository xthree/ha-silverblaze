"""The issue form and pull request template carry the required fields."""

from __future__ import annotations

from pathlib import Path

import yaml

GITHUB = Path(__file__).parent.parent / ".github"
FORM = GITHUB / "ISSUE_TEMPLATE" / "utility-support-request.yml"


def _fields() -> dict[str, dict]:
    form = yaml.safe_load(FORM.read_text(encoding="utf-8"))
    return {item["id"]: item for item in form["body"] if "id" in item}


def test_form_name_and_notice():
    form = yaml.safe_load(FORM.read_text(encoding="utf-8"))
    assert form["name"] == "Utility support request"
    first = form["body"][0]
    assert first["type"] == "markdown"
    text = first["attributes"]["value"].lower()
    assert "password" in text and "account number" in text and "meter id" in text


def test_required_inputs():
    f = _fields()
    for key in ("utility-name", "portal-url", "probe-report"):
        assert f[key]["validations"]["required"] is True
    assert f["probe-report"]["type"] == "textarea"
    assert "review" in f["probe-report"]["attributes"]["description"].lower()


def test_commodities_checkboxes():
    labels = [o["label"] for o in _fields()["commodities"]["attributes"]["options"]]
    assert labels == ["Electric", "Gas", "Water", "Other"]


def test_review_checkbox_required():
    (opt,) = _fields()["report-reviewed"]["attributes"]["options"]
    assert "no personal data" in opt["label"]
    assert opt["required"] is True


def test_willing_dropdown():
    field = _fields()["willing-to-test"]
    assert field["type"] == "dropdown"
    assert field["attributes"]["options"] == ["Yes", "No"]


def test_chooser_allows_blank_issues():
    cfg = yaml.safe_load((GITHUB / "ISSUE_TEMPLATE" / "config.yml").read_text(encoding="utf-8"))
    assert cfg["blank_issues_enabled"] is True


def test_pr_template_checkboxes():
    text = (GITHUB / "pull_request_template.md").read_text(encoding="utf-8")
    assert "- [ ] Fixtures redacted/synthetic" in text
    assert "- [ ] Live-verified with HA (backfill + one update cycle)" in text
    assert "by: ___" in text
    assert "- [ ] Tests added" in text
