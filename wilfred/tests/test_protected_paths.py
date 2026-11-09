"""The forked sensitive/irreversible path classifier (read-side guardrail).

Pins the fork's behaviour AND the fork-not-import discipline: this module must classify the
right categories and route to the right gate kind, with ZERO dependence on common_py.factory.
"""

from __future__ import annotations

from hyperwilfred_core import protected_paths as pp


def test_classifies_each_sensitive_category() -> None:
    gate = pp.ProtectedPathGate()
    cases = {
        "secrets": ".env",
        "migrations": "app/migrations/0001_init.py",
        "payments": "src/billing/charge.py",
        "auth": "src/auth/login.py",
        "infra": ".github/workflows/deploy.yml",
        "contracts": "contracts/Token.sol",
        "compliance": "src/compliance/gdpr.py",
    }
    for category, path in cases.items():
        result = gate.check_paths([path])
        assert result.requires_human_review is True, path
        assert category in result.categories, (path, result.categories)


def test_benign_paths_need_no_review() -> None:
    result = pp.ProtectedPathGate().check_paths(
        ["product/lib.py", "src/utils/format.py", "README.md", "tests/test_lib.py"]
    )
    assert result.requires_human_review is False
    assert result.categories == []
    assert result.irreversible is False


def test_irreversible_categories_flagged() -> None:
    res = pp.ProtectedPathGate().check_paths(["db/migrations/0002.sql"])
    assert res.irreversible is True
    res2 = pp.ProtectedPathGate().check_paths(["src/auth/jwt.py"])
    # auth is sensitive but NOT in the irreversible set
    assert res2.requires_human_review is True
    assert res2.irreversible is False


def test_category_to_gate_kind_routing() -> None:
    assert pp.category_to_gate_kind("secrets") == "secrets"
    assert pp.category_to_gate_kind("migrations") == "migration"
    assert pp.category_to_gate_kind("payments") == "deploy"
    assert pp.category_to_gate_kind("infra") == "deploy"
    # a non-irreversible sensitive category still routes to a human-review kind, never empty
    assert pp.category_to_gate_kind("auth") == "secrets"
    assert pp.category_to_gate_kind("unknown-cat") == "secrets"


def test_dotenv_variants_caught() -> None:
    gate = pp.ProtectedPathGate()
    for p in (".env", ".env.production", "config/.env", "deep/nested/.env.local"):
        assert gate.check_paths([p]).requires_human_review is True, p


def test_key_material_and_ci_files_classified() -> None:
    """Regression (verifier #7): real secret/infra files that previously slipped through."""
    gate = pp.ProtectedPathGate()
    secrets = ["server/id_rsa", "deploy/id_ed25519", "certs/server.key", "x/cert.pem",
               "app/.npmrc", "release/.pypirc", "tls/store.p12"]
    for p in secrets:
        assert "secrets" in gate.check_paths([p]).categories, p
    infra = ["ci/.gitlab-ci.yml", "Jenkinsfile", "docker/Dockerfile.prod", "ops/kubeconfig"]
    for p in infra:
        assert "infra" in gate.check_paths([p]).categories, p


def test_overlay_extends_without_losing_defaults() -> None:
    cfg = pp.ProtectedPathConfig.with_overlay({"custom": ("**/sacred/**",)})
    gate = pp.ProtectedPathGate(cfg)
    assert gate.check_paths(["x/sacred/y.py"]).categories == ["custom"]
    # defaults still present
    assert gate.check_paths([".env"]).requires_human_review is True


def test_module_has_no_common_py_dependency() -> None:
    """The fork must be importable with common_py absent (sanity beyond the AST guard)."""
    src = (
        pp.__file__
    )
    text = open(src, encoding="utf-8").read()
    # provenance docstring mention is fine; a real import is not
    for line in text.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("import common_py")
        assert not stripped.startswith("from common_py")
