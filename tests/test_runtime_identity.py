"""Runtime identity: running version and hash of the exact config bytes read."""

import hashlib
import importlib
from importlib import metadata

import ops_engine
from ops_engine import config_loader

ri_module = importlib.import_module("ops_engine.runtime_identity")
UNKNOWN = ri_module.UNKNOWN
runtime_identity = ri_module.runtime_identity


def _reset():
    config_loader._reset_last_config_sha256()


def test_version_comes_from_importlib_metadata_at_runtime():
    assert runtime_identity().version == metadata.version("ops-engine")


def test_hash_is_sha256_of_exact_bytes_the_loader_read(tmp_path):
    _reset()
    raw = b"orgs:\r\n  acme: {}\n# trailing bytes \xe2\x9c\x93\n"
    path = tmp_path / ".ops.yaml"
    path.write_bytes(raw)
    config_loader.load_ops_yaml(tmp_path)
    assert runtime_identity().config_sha256 == hashlib.sha256(raw).hexdigest()


def test_load_config_file_records_hash_of_read_bytes(tmp_path):
    _reset()
    raw = b"orgs:\n  acme: {}\n"
    path = tmp_path / "config.yml"
    path.write_bytes(raw)
    cfg = config_loader.load_config_file(path)
    assert "acme" in cfg.orgs
    assert runtime_identity().config_sha256 == hashlib.sha256(raw).hexdigest()


def test_config_hash_is_unknown_when_no_config_was_read():
    _reset()
    assert runtime_identity().config_sha256 == UNKNOWN == "unknown"


def test_version_unknown_when_metadata_missing_and_never_raises(monkeypatch):
    def boom(_name):
        raise metadata.PackageNotFoundError("ops-engine")

    monkeypatch.setattr(ri_module.metadata, "version", boom)
    assert runtime_identity().version == "unknown"


def test_never_raises_on_arbitrary_failures(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(ri_module.metadata, "version", boom)
    monkeypatch.setattr(ri_module, "last_config_sha256", boom)
    ident = runtime_identity()
    assert ident.version == "unknown"
    assert ident.config_sha256 == "unknown"


def test_public_export():
    assert ops_engine.runtime_identity is runtime_identity


def test_hash_names_loaded_config_after_file_is_replaced_on_disk(tmp_path):
    """RED proof: the reported hash is of the bytes read, not of the file now on disk.

    An implementation that re-hashes the file at report time would name config B
    here and fail.
    """
    _reset()
    raw_a = b"orgs:\n  acme: {}\n"
    raw_b = b"orgs:\n  other: {}\n# replaced after load\n"
    path = tmp_path / "config.yml"
    path.write_bytes(raw_a)
    config_loader.load_config_file(path)

    path.write_bytes(raw_b)

    reported = runtime_identity().config_sha256
    assert reported == hashlib.sha256(raw_a).hexdigest()
    assert reported != hashlib.sha256(raw_b).hexdigest()
    assert reported != hashlib.sha256(path.read_bytes()).hexdigest()
