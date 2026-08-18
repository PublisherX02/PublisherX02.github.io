"""Sanity check that the firewall package is importable."""

import firewall.audit
import firewall.policy
import firewall.proxy


def test_package_imports():
    assert firewall.proxy
    assert firewall.policy
    assert firewall.audit
