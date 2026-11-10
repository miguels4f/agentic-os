"""One stable regular-file reader guards every trusted read.

The link-free walk, the no-follow open, the identity check and the final re-check live once in
``path_safety.stable_regular_bytes``; quality, approvals, regression and control evolution only
translate its single refusal type into their own errors. Every file here is a temporary fixture.
"""

import inspect
import os
import re
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyperwilfred_core import approvals, control_evolution, path_safety, quality, regression

READER_MODULES = (quality, approvals, regression, control_evolution)
NOFOLLOW_READ = re.compile(r"O_RDONLY\s*\|\s*(os\.O_NOFOLLOW|getattr\(os,\s*\"O_NOFOLLOW\")")


def _pem(path: Path) -> Path:
    public = Ed25519PrivateKey.generate().public_key()
    path.write_bytes(public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return path


def _replace_on_first(monkeypatch, name: str, path: Path, content: bytes) -> list[bool]:
    """Atomically swap *path* for a new inode the first time ``os.<name>`` runs."""
    real = getattr(os, name)
    fired: list[bool] = []

    def swapping(*args, **kwargs):
        if not fired:
            fired.append(True)
            fresh = path.with_name(path.name + ".fresh")
            fresh.write_bytes(content)
            os.replace(fresh, path)
        return real(*args, **kwargs)

    monkeypatch.setattr(os, name, swapping)
    return fired


# --------------------------------------------------------------------------- shared primitive


def test_reader_returns_exact_bytes_of_a_regular_file(tmp_path):
    target = tmp_path / "a" / "b.txt"
    target.parent.mkdir()
    target.write_bytes(b"payload\n")
    assert path_safety.stable_regular_bytes(target) == b"payload\n"
    assert path_safety.stable_regular_bytes(target, root=tmp_path) == b"payload\n"
    assert issubclass(path_safety.UnstableFileError, ValueError)
    assert "stable_regular_bytes" in path_safety.__all__
    assert "UnstableFileError" in path_safety.__all__


@pytest.mark.parametrize("kind", ["leaf", "dangling", "parent", "cycle"])
def test_reader_refuses_every_symlink_below_the_root(tmp_path, kind):
    real = tmp_path / "real"
    real.mkdir()
    (real / "f.txt").write_text("x")
    if kind == "leaf":
        path = tmp_path / "link.txt"
        path.symlink_to(real / "f.txt")
    elif kind == "dangling":
        path = tmp_path / "link.txt"
        path.symlink_to(tmp_path / "missing.txt")
    elif kind == "parent":
        (tmp_path / "alias").symlink_to(real, target_is_directory=True)
        path = tmp_path / "alias" / "f.txt"
    else:
        path = tmp_path / "loop.txt"
        path.symlink_to(path)
    with pytest.raises(path_safety.UnstableFileError, match="contains symlink") as caught:
        path_safety.stable_regular_bytes(path, root=tmp_path, label="probe")
    assert caught.value.reason == "symlink"


def test_reader_refuses_missing_unless_allowed(tmp_path):
    absent = tmp_path / "gone" / "f.txt"
    with pytest.raises(path_safety.UnstableFileError, match="probe is missing") as caught:
        path_safety.stable_regular_bytes(absent, root=tmp_path, label="probe")
    assert caught.value.reason == "missing"
    assert path_safety.stable_regular_bytes(absent, root=tmp_path, allow_missing=True) is None


def test_reader_refuses_escape_and_non_regular_files(tmp_path):
    inside, outside = tmp_path / "in", tmp_path / "out"
    inside.mkdir()
    outside.mkdir()
    (outside / "f.txt").write_text("x")
    with pytest.raises(path_safety.UnstableFileError, match="escapes its trusted root"):
        path_safety.stable_regular_bytes(outside / "f.txt", root=inside, label="probe")
    with pytest.raises(path_safety.UnstableFileError, match="not a regular file") as caught:
        path_safety.stable_regular_bytes(inside, root=tmp_path, label="probe")
    assert caught.value.reason == "not-regular"


def test_reader_refuses_replacement_before_open(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"
    target.write_bytes(b"original")
    fired = _replace_on_first(monkeypatch, "open", target, b"attacker")
    with pytest.raises(path_safety.UnstableFileError, match="changed before it was opened"):
        path_safety.stable_regular_bytes(target, label="probe")
    assert fired


def test_reader_refuses_replacement_during_read(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"
    target.write_bytes(b"original")
    fired = _replace_on_first(monkeypatch, "read", target, b"attacker")
    with pytest.raises(path_safety.UnstableFileError, match="changed while it was read") as caught:
        path_safety.stable_regular_bytes(target, label="probe")
    assert fired and caught.value.reason == "changed"


def test_reader_refuses_in_place_growth_during_read(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"
    target.write_bytes(b"original")
    real = os.read
    grown: list[bool] = []

    def growing(descriptor, size):
        if not grown:
            grown.append(True)
            with target.open("ab") as stream:
                stream.write(b"-appended")
        return real(descriptor, size)

    monkeypatch.setattr(os, "read", growing)
    with pytest.raises(path_safety.UnstableFileError, match="changed while it was read"):
        path_safety.stable_regular_bytes(target, label="probe")
    assert grown


def test_reader_opens_without_following_a_leaf_swapped_to_a_link(tmp_path, monkeypatch):
    target, elsewhere = tmp_path / "f.txt", tmp_path / "elsewhere.txt"
    target.write_bytes(b"original")
    elsewhere.write_bytes(b"attacker")
    real = os.open
    swapped: list[bool] = []

    def swap_then_open(path, flags, *args, **kwargs):
        if not swapped:
            swapped.append(True)
            target.unlink()
            target.symlink_to(elsewhere)
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_then_open)
    with pytest.raises(path_safety.UnstableFileError) as caught:
        path_safety.stable_regular_bytes(target, label="probe")
    assert swapped and caught.value.reason in {"unopenable", "replaced"}


# --------------------------------------------------------------------------- one implementation


def test_no_caller_keeps_its_own_copy_of_the_reader():
    for module in READER_MODULES:
        source = inspect.getsource(module)
        assert not NOFOLLOW_READ.search(source), module.__name__
        assert "os.read(" not in source and "st_ctime_ns" not in source, module.__name__


def test_every_boundary_reads_through_the_shared_primitive(tmp_path, monkeypatch):
    calls: list[Path] = []
    real = path_safety.stable_regular_bytes

    def spy(path, **kwargs):
        calls.append(Path(path))
        return real(path, **kwargs)

    monkeypatch.setattr(path_safety, "stable_regular_bytes", spy)
    plain = tmp_path / "plain.txt"
    plain.write_bytes(b"x")
    key = _pem(tmp_path / "key.pem")
    witness = tmp_path / "pkg" / "test_w.py"
    witness.parent.mkdir()
    witness.write_text("def test_w():\n    assert True\n")

    assert quality._stable_regular_bytes(plain, label="plain") == b"x"
    assert approvals.public_key_fingerprint(key)
    assert regression._witness_bytes(witness, witness.parent).startswith(b"def test_w")
    assert control_evolution._read(plain) == b"x"
    assert calls == [plain, key, witness, plain]


# --------------------------------------------------------------------------- boundary wording


def test_quality_boundary_keeps_its_error_type_and_wording(tmp_path):
    (tmp_path / "real.txt").write_text("x")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    with pytest.raises(quality.QualityError, match="probe contains symlink"):
        quality._stable_regular_bytes(tmp_path / "link.txt", label="probe")
    with pytest.raises(quality.QualityError, match="probe is missing"):
        quality._stable_regular_bytes(tmp_path / "none.txt", label="probe")
    assert quality._stable_regular_bytes(tmp_path / "none.txt", allow_missing=True) is None


def test_approval_key_boundary_refuses_links_replacement_and_absence(tmp_path, monkeypatch):
    real = _pem(tmp_path / "real.pem")
    link = tmp_path / "link.pem"
    link.symlink_to(real)
    with pytest.raises(approvals.ApprovalError, match="is not a regular file"):
        approvals.public_key_fingerprint(link)
    with pytest.raises(approvals.ApprovalError, match="unavailable at"):
        approvals.public_key_fingerprint(tmp_path / "absent.pem")
    fired = _replace_on_first(monkeypatch, "read", real, _pem(tmp_path / "other.pem").read_bytes())
    with pytest.raises(approvals.ApprovalError, match="key changed while it was read"):
        approvals.public_key_fingerprint(real)
    assert fired


def test_regression_witness_boundary_refuses_links_replacement_and_absence(tmp_path, monkeypatch):
    target = tmp_path / "pkg"
    (target / "real").mkdir(parents=True)
    (target / "real" / "test_a.py").write_text("def test_a():\n    assert True\n")
    (target / "alias").symlink_to(target / "real", target_is_directory=True)
    with pytest.raises(regression.RegressionError, match="pytest witness must not be a symlink"):
        regression._witness_bytes(target / "alias" / "test_a.py", target)
    with pytest.raises(OSError):
        regression._witness_bytes(target / "real" / "test_missing.py", target)
    witness = target / "real" / "test_a.py"
    fired = _replace_on_first(monkeypatch, "read", witness, b"def test_a():\n    pass\n")
    with pytest.raises(regression.RegressionError, match="changed while being read"):
        regression._witness_bytes(witness, target)
    assert fired


def test_regression_witness_refuses_a_symlinked_target_root(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "test_a.py").write_text("def test_a():\n    assert True\n")
    (tmp_path / "pkg").symlink_to(real, target_is_directory=True)
    with pytest.raises(regression.RegressionError, match="must not be a symlink"):
        regression._witness_bytes(tmp_path / "pkg" / "test_a.py", tmp_path / "pkg")


def test_control_evidence_boundary_refuses_links(tmp_path):
    (tmp_path / "real.json").write_text("{}")
    (tmp_path / "link.json").symlink_to(tmp_path / "real.json")
    with pytest.raises(control_evolution.ControlEvolutionError, match="symlink"):
        control_evolution._read(tmp_path / "link.json")
    with pytest.raises(control_evolution.ControlEvolutionError, match="is missing"):
        control_evolution._read(tmp_path / "absent.json")
