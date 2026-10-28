"""The docs that are generated from the code, or have to stay valid against it."""

from pathlib import Path

import pytest

from busybar_calendar import config, reference

ROOT = Path(__file__).resolve().parent.parent
MODELS = [
    config.BarConfig,
    config.SyncConfig,
    config.DisplayConfig,
    config.ReminderSettings,
    config.StatusSettings,
    config.SourceConfig,
    config.TitleRule,
]


def test_configuration_reference_is_current():
    committed = (ROOT / "docs" / "configuration.md").read_text()
    assert committed == reference.markdown(), (
        "docs/configuration.md is out of date: "
        "python -m busybar_calendar.reference > docs/configuration.md"
    )


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_every_option_is_described(model):
    overrides = model in (config.SourceConfig, config.TitleRule)
    inherited = {"reminder", "status"} if overrides else set()
    missing = [
        name
        for name, field in model.model_fields.items()
        if not field.description and name not in inherited
    ]
    assert not missing, f"{model.__name__}: describe {missing} - it goes into the reference"


def test_example_config_loads():
    loaded = config.load(ROOT / "config.example.toml")
    assert loaded.sources


DOCS = [ROOT / "README.md", ROOT / "CHANGELOG.md", *sorted((ROOT / "docs").glob("*.md"))]


@pytest.mark.parametrize("doc", DOCS, ids=lambda path: path.name)
def test_relative_links_resolve(doc):
    import re

    targets = re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", doc.read_text())
    broken = [
        target
        for target in targets
        if not target.startswith(("http://", "https://", "mailto:"))
        and not (doc.parent / target).exists()
    ]
    assert not broken, f"{doc.name} links to missing files: {broken}"
