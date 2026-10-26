"""Keep the 0.12 execution boundary consistent beyond the README opening."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    "relative",
    [
        "README.md",
        "docs/declarative-capabilities.md",
        "examples/README.md",
        "src/summonpot/AGENTS.md",
        "src/summonpot/templates/skills/summonpot.md",
        "website/src/content/docs/architecture.mdx",
        "website/src/content/docs/internals/execution.mdx",
        "website/src/content/docs/reference/operations.mdx",
        "website/src/content/docs/tasks/direct-execution.mdx",
    ],
)
def test_model_free_chain_contract_reaches_every_explanatory_surface(relative):
    text = " ".join((ROOT / relative).read_text().split())
    for term in ("0.12", "FromRequest", "FromResult", "Exactly(1)", "AgentChoice"):
        assert term in text, (relative, term)
    assert "model-free" in text or "without a model" in text
    assert "Pydantic request" in text
    assert "terminal" in text and "annotation" in text
    for stale in (
        "The chain remains agent-mediated even without",
        "including chains without an agent-owned argument; it is not model-free",
        "no general dependency graph or model-free multi-operation execution is supported",
        "Context and broader bounds are unsupported; the chain is agent-mediated, not model-free",
        "multi-operation direct paths remain planned",
    ):
        assert stale not in text, (relative, stale)


def test_linear_example_no_longer_calls_the_012_path_unreleased():
    text = (ROOT / "examples/11_linear_result_chain/README.md").read_text()
    assert "included from 0.12.0" in text
    assert "unreleased" not in text.lower()


def test_012_release_notes_include_both_consumed_changes():
    text = (ROOT / "CHANGELOG.md").read_text()
    notes = re.split(r"## \[0\.12\.0\].*\n", text, maxsplit=1)[1].split(
        "## [0.11.0]", 1
    )[0]
    assert "model-free" in notes
    assert "producer-choice chain diagram" in notes
