import ipaddress

import pytest

from pr_agent.algo import url_safety


@pytest.mark.parametrize("address", ["100.64.0.1", "100.127.255.254", "ff02::1", "fec0::1",
                                     "::ffff:10.0.0.1", "::ffff:100.64.0.1", "64:ff9b::a00:1",
                                     "64:ff9b::7f00:1", "64:ff9b::6440:1", "64:ff9b::e000:fb",
                                     "64:ff9b:1::808:808"])
def test_nonpublic_address_ranges_are_blocked(address):
    assert url_safety.ip_is_blocked(ipaddress.ip_address(address))
