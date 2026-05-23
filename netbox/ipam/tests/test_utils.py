from django.test import SimpleTestCase
from netaddr import IPAddress, IPNetwork

from ipam.utils import _to_ipaddress


class ToIPAddressTestCase(SimpleTestCase):
    """
    Coverage for the internal _to_ipaddress() value normalizer.
    """

    def test_passes_through_ipaddress(self):
        ip = IPAddress('192.0.2.1')
        self.assertIs(_to_ipaddress(ip), ip)

    def test_extracts_ip_from_ipnetwork(self):
        self.assertEqual(_to_ipaddress(IPNetwork('192.0.2.1/24')), IPAddress('192.0.2.1'))

    def test_parses_plain_ipv4_host_string(self):
        # Fast path: IPAddress() succeeds, IPNetwork() construction is skipped.
        self.assertEqual(_to_ipaddress('192.0.2.1'), IPAddress('192.0.2.1'))

    def test_parses_plain_ipv6_host_string(self):
        self.assertEqual(_to_ipaddress('2001:db8::1'), IPAddress('2001:db8::1'))

    def test_falls_back_to_ipnetwork_for_cidr_string(self):
        # IPAddress('192.0.2.0/24') raises AddrFormatError; the fallback parses
        # the value as an IPNetwork and returns its host portion.
        self.assertEqual(_to_ipaddress('192.0.2.0/24'), IPAddress('192.0.2.0'))
