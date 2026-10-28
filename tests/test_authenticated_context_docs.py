from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_authenticated_context_docs_preserve_trust_boundary_and_release_history():
    files = [
        ROOT / "README.md",
        ROOT / "ROADMAP.md",
        ROOT / "docs/declarative-capabilities.md",
        ROOT / "website/src/content/docs/reference/operations.mdx",
        ROOT / "src/summonpot/templates/skills/summonpot.md",
    ]
    for path in files:
        text = path.read_text()
        assert "FromContext" in text
        assert "0.12.0" in text
        assert "tenant" in text.lower()
        assert "model" in text.lower()
    skill = files[-1].read_text()
    for phrase in (
        "secrets.compare_digest",
        "frozen=True",
        'extra="forbid"',
        "not part of published 0.12.0",
        "trusted application code",
        "tenant isolation",
        "runtime timeout",
    ):
        assert phrase in skill


def test_no_affected_surface_calls_context_globally_unsupported():
    paths = [
        ROOT / "README.md",
        ROOT / "ROADMAP.md",
        *ROOT.joinpath("docs").rglob("*.md"),
        *ROOT.joinpath("website/src/content/docs").rglob("*.mdx"),
        ROOT / "examples/README.md",
        ROOT / "src/summonpot/templates/skills/summonpot.md",
    ]
    for path in paths:
        text = path.read_text()
        assert "FromContext remains unsupported" not in text, path
        assert "`FromContext` remains unsupported" not in text, path
