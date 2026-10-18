"""is_private_host: run with `./venv/bin/python -m unittest test_common`."""

import socket
import unittest
from unittest import mock

from tau_search import common


def _resolving(*addrs):
    fam = {4: socket.AF_INET, 6: socket.AF_INET6}
    return mock.patch.object(common.socket, "getaddrinfo", return_value=[
        (fam[6 if ":" in a else 4], socket.SOCK_STREAM, 6, "", (a, 0)) for a in addrs])


class PrivateHost(unittest.TestCase):
    def test_literal_non_public_addresses(self):
        for host in ("127.0.0.1", "10.1.2.3", "192.168.1.5", "172.16.0.1",
                     "172.31.255.254", "100.64.0.1", "169.254.169.254", "0.0.0.0",
                     "::1", "[::1]", "fe80::1", "fd00::1"):
            with self.subTest(host=host):
                self.assertTrue(common.is_private_host(host))

    def test_the_old_prefix_checks_both_ways(self):
        # 172.16/12 was a literal "172.1[6-9]." string and never matched; and a
        # "172.2" prefix wrongly caught public 172.200.x.
        self.assertTrue(common.is_private_host("172.17.0.1"))
        with _resolving("172.200.1.1"):
            self.assertFalse(common.is_private_host("example.test"))

    def test_names(self):
        for host in ("localhost", "printer.local", "db.internal"):
            self.assertTrue(common.is_private_host(host))

    def test_a_name_with_any_private_address_is_private(self):
        with _resolving("93.184.216.34", "127.0.0.1"):
            self.assertTrue(common.is_private_host("mixed.test"))
        with _resolving("93.184.216.34", "2606:2800:220:1::1"):
            self.assertFalse(common.is_private_host("public.test"))


if __name__ == "__main__":
    unittest.main()
