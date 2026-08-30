"""Smoke test: the src-layout package installs and imports cleanly."""


def test_package_imports():
    import deepseek_balance

    assert deepseek_balance.__version__
