"""Tests for src.config.get_business_description loader."""

import os
import textwrap

import pytest

from src.config import get_business_description, reset_business_descriptions_cache


@pytest.fixture(autouse=True)
def reset_cache():
    reset_business_descriptions_cache()
    yield
    reset_business_descriptions_cache()


def _write_yaml(monkeypatch, tmp_path, content: str):
    """Patch the loader to read from `tmp_path/business_descriptions.yaml`."""
    p = tmp_path / "business_descriptions.yaml"
    p.write_text(textwrap.dedent(content))
    fake_dirname = lambda _: str(tmp_path / "src_dir")
    real_join = os.path.join

    def fake_join(*parts):
        joined = real_join(*parts)
        if joined.endswith("config/business_descriptions.yaml"):
            return str(p)
        return joined

    monkeypatch.setattr("src.config.os.path.dirname", fake_dirname)
    monkeypatch.setattr("src.config.os.path.join", fake_join)


def test_known_symbol_returns_description(monkeypatch, tmp_path):
    _write_yaml(monkeypatch, tmp_path, """
        descriptions:
          HII: "Naval shipbuilder: aircraft carriers, submarines, destroyers"
          XOM: "Integrated oil major: upstream + downstream refining"
    """)
    assert get_business_description("HII").startswith("Naval shipbuilder")
    assert "oil major" in get_business_description("XOM")


def test_unknown_symbol_returns_none(monkeypatch, tmp_path):
    _write_yaml(monkeypatch, tmp_path, """
        descriptions:
          HII: "Naval shipbuilder"
    """)
    assert get_business_description("ZZZZ") is None


def test_empty_descriptions_block(monkeypatch, tmp_path):
    _write_yaml(monkeypatch, tmp_path, """
        descriptions: {}
    """)
    assert get_business_description("HII") is None


def test_missing_file_returns_none(monkeypatch, tmp_path):
    fake_dirname = lambda _: str(tmp_path / "src_dir")
    monkeypatch.setattr("src.config.os.path.dirname", fake_dirname)
    # No file written → YAML load raises FileNotFoundError → empty cache
    assert get_business_description("HII") is None


def test_malformed_yaml(monkeypatch, tmp_path):
    _write_yaml(monkeypatch, tmp_path, """
        descriptions: [this should be a dict not a list]
    """)
    assert get_business_description("HII") is None


def test_blank_or_none_value_skipped(monkeypatch, tmp_path):
    _write_yaml(monkeypatch, tmp_path, """
        descriptions:
          HII: ""
          LHX: null
          XOM: "Oil major"
    """)
    assert get_business_description("HII") is None
    assert get_business_description("LHX") is None
    assert get_business_description("XOM") == "Oil major"


def test_strip_whitespace_in_lookup(monkeypatch, tmp_path):
    _write_yaml(monkeypatch, tmp_path, """
        descriptions:
          HII: "Naval shipbuilder"
    """)
    assert get_business_description("  HII  ") == "Naval shipbuilder"


def test_empty_string_symbol(monkeypatch, tmp_path):
    _write_yaml(monkeypatch, tmp_path, """
        descriptions:
          HII: "Naval shipbuilder"
    """)
    assert get_business_description("") is None
    assert get_business_description(None) is None
