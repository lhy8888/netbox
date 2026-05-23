import heapq
from dataclasses import dataclass

import netaddr
from django.apps import apps
from django.db.models import Q, QuerySet
from django.db.models.functions import Cast
from django.utils.translation import gettext_lazy as _

from .constants import *
from .fields import IPAddressField
from .lookups import Host

__all__ = (
    'AvailableIPSpace',
    'add_available_vlans',
    'add_requested_prefixes',
    'annotate_host_address',
    'annotate_ip_space',
    'count_distinct_ip_hosts',
    'count_distinct_ip_hosts_outside_intervals',
    'count_ip_intervals',
    'filter_ip_hosts_between',
    'find_first_available_ip',
    'get_iprange_intervals',
    'get_next_available_prefix',
    'get_usable_ip_bounds',
    'merge_ip_intervals',
    'rebuild_prefixes',
)


@dataclass
class AvailableIPSpace:
    """
    A representation of available IP space between two IP addresses/ranges.
    """
    size: int
    first_ip: str

    @property
    def title(self):
        if self.size == 1:
            return _('1 IP available')
        if self.size <= 65536:
            return _('{count} IPs available').format(count=self.size)
        return _('Many IPs available')


def get_usable_ip_bounds(prefix):
    """
    Return the first and last IPs considered usable for available-IP calculations.

    Pools and IPv4 /31-/32 / IPv6 /127-/128 are fully usable; otherwise IPv4 excludes
    network and broadcast, IPv6 excludes the subnet-router anycast address.
    """
    family = prefix.family
    first = prefix.prefix.first
    last = prefix.prefix.last
    mask_length = prefix.mask_length

    if (
        prefix.is_pool
        or (family == 4 and mask_length >= 31)
        or (family == 6 and mask_length >= 127)
    ):
        return (
            netaddr.IPAddress(first, version=family),
            netaddr.IPAddress(last, version=family),
        )

    if family == 4:
        return (
            netaddr.IPAddress(first + 1, version=family),
            netaddr.IPAddress(last - 1, version=family),
        )

    return (
        netaddr.IPAddress(first + 1, version=family),
        netaddr.IPAddress(last, version=family),
    )


def add_requested_prefixes(parent, prefix_list, show_available=True, show_assigned=True):
    """
    Return a list of requested prefixes using show_available, show_assigned filters. If available prefixes are
    requested, create fake Prefix objects for all unallocated space within a prefix.

    :param parent: Parent Prefix instance
    :param prefix_list: Child prefixes list (or queryset)
    :param show_available: Include available prefixes.
    :param show_assigned: Show assigned prefixes.
    """
    child_prefixes = []

    # Add available prefixes to the table if requested
    if prefix_list and show_available:
        # Infer the Prefix model from the queryset/list so this helper does not need
        # a model import at module scope.
        prefix_model = getattr(prefix_list, 'model', None) or prefix_list[0].__class__

        # Find all unallocated space, add fake Prefix objects to child_prefixes.
        # IMPORTANT: These are unsaved Prefix instances (pk=None). If this is ever changed to use
        # saved Prefix instances with real pks, bulk delete will fail for mixed-type selections
        # due to single-model form validation. See: https://github.com/netbox-community/netbox/issues/21176
        available_prefixes = netaddr.IPSet(parent) ^ netaddr.IPSet([p.prefix for p in prefix_list])
        available_prefixes = [prefix_model(prefix=p, status=None) for p in available_prefixes.iter_cidrs()]
        child_prefixes = child_prefixes + available_prefixes

    # Add assigned prefixes to the table if requested
    if prefix_list and show_assigned:
        child_prefixes = child_prefixes + list(prefix_list)

    # Sort child prefixes after additions
    child_prefixes.sort(key=lambda p: p.prefix)

    return child_prefixes


def annotate_ip_space(prefix):
    # Compile child objects
    records = []
    records.extend([
        (iprange.start_address.ip, iprange) for iprange in prefix.get_child_ranges(mark_populated=True)
    ])
    records.extend([
        (ip.address.ip, ip) for ip in prefix.get_child_ips()
    ])
    records = sorted(records, key=lambda x: x[0])

    # Determine the first & last valid IP addresses in the prefix
    first_ip_in_prefix, last_ip_in_prefix = get_usable_ip_bounds(prefix)

    if not records:
        return [
            AvailableIPSpace(
                size=int(last_ip_in_prefix - first_ip_in_prefix + 1),
                first_ip=f'{first_ip_in_prefix}/{prefix.mask_length}'
            )
        ]

    output = []
    prev_ip = None

    # Account for any available IPs before the first real IP
    if records[0][0] > first_ip_in_prefix:
        output.append(AvailableIPSpace(
            size=int(records[0][0] - first_ip_in_prefix),
            first_ip=f'{first_ip_in_prefix}/{prefix.mask_length}'
        ))

    # Add IP ranges & addresses, annotating available space in between records
    for record in records:
        if prev_ip:
            # Annotate available space
            if (diff := int(record[0]) - int(prev_ip)) > 1:
                first_skipped = f'{prev_ip + 1}/{prefix.mask_length}'
                output.append(AvailableIPSpace(
                    size=diff - 1,
                    first_ip=first_skipped
                ))

        output.append(record[1])

        # Update the previous IP address
        if hasattr(record[1], 'end_address'):
            prev_ip = record[1].end_address.ip
        else:
            prev_ip = record[0]

    # Include any remaining available IPs
    if prev_ip < last_ip_in_prefix:
        output.append(AvailableIPSpace(
            size=int(last_ip_in_prefix - prev_ip),
            first_ip=f'{prev_ip + 1}/{prefix.mask_length}'
        ))

    return output


def available_vlans_from_range(vlans, vlan_group, vid_range):
    """
    Create fake records for all gaps between used VLANs
    """
    min_vid = int(vid_range.lower) if vid_range else VLAN_VID_MIN
    max_vid = int(vid_range.upper) if vid_range else VLAN_VID_MAX

    if not vlans:
        return [{
            'vid': min_vid,
            'vlan_group': vlan_group,
            'available': max_vid - min_vid
        }]

    prev_vid = min_vid - 1
    new_vlans = []
    for vlan in vlans:

        # Ignore VIDs outside the range
        if not min_vid <= vlan.vid < max_vid:
            continue

        # Annotate any available VIDs between the previous (or minimum) VID
        # and the current VID
        if vlan.vid - prev_vid > 1:
            new_vlans.append({
                'vid': prev_vid + 1,
                'vlan_group': vlan_group,
                'available': vlan.vid - prev_vid - 1,
            })

        prev_vid = vlan.vid

    # Annotate any remaining available VLANs
    if prev_vid < max_vid - 1:
        new_vlans.append({
            'vid': prev_vid + 1,
            'vlan_group': vlan_group,
            'available': max_vid - prev_vid - 1,
        })

    return new_vlans


def add_available_vlans(vlans, vlan_group):
    """
    Create fake records for all gaps between used VLANs
    """
    new_vlans = []
    for vid_range in vlan_group.vid_ranges:
        new_vlans.extend(available_vlans_from_range(vlans, vlan_group, vid_range))

    vlans = list(vlans) + new_vlans
    vlans.sort(key=lambda v: v['vid'] if isinstance(v, dict) else v.vid)

    return vlans


def rebuild_prefixes(prefixes):
    """
    Rebuild the prefix hierarchy for the supplied Prefix queryset.

    For backward compatibility with callers that pass a VRF identifier (None for the
    global table, or a VRF pk), a non-QuerySet argument is treated as a VRF filter.
    """
    if isinstance(prefixes, QuerySet):
        prefix_queryset = prefixes
        prefix_model = prefixes.model
    else:
        prefix_model = apps.get_model('ipam', 'Prefix')
        prefix_queryset = prefix_model.objects.filter(vrf=prefixes)

    def contains(parent, child):
        return child in parent and child != parent

    def push_to_stack(prefix):
        # Increment child count on parent nodes
        for n in stack:
            n['children'] += 1
        stack.append({
            'pk': [prefix['pk']],
            'prefix': prefix['prefix'],
            'children': 0,
        })

    stack = []
    update_queue = []
    prefixes = prefix_queryset.order_by('prefix', 'pk').values('pk', 'prefix')

    # Iterate through all Prefixes in the table, growing and shrinking the stack as we go
    for p in prefixes:

        # Grow the stack if this is a child of the most recent prefix
        if not stack or contains(stack[-1]['prefix'], p['prefix']):
            push_to_stack(p)

        # Handle duplicate prefixes
        elif stack[-1]['prefix'] == p['prefix']:
            stack[-1]['pk'].append(p['pk'])

        # If this is a sibling or parent of the most recent prefix, pop nodes from the
        # stack until we reach a parent prefix (or the root)
        else:
            while stack and not contains(stack[-1]['prefix'], p['prefix']):
                node = stack.pop()
                for pk in node['pk']:
                    update_queue.append(
                        prefix_model(pk=pk, _depth=len(stack), _children=node['children'])
                    )
            push_to_stack(p)

        # Flush the update queue once it reaches 100 Prefixes
        if len(update_queue) >= 100:
            prefix_model.objects.bulk_update(update_queue, ['_depth', '_children'])
            update_queue = []

    # Clear out any prefixes remaining in the stack
    while stack:
        node = stack.pop()
        for pk in node['pk']:
            update_queue.append(
                prefix_model(pk=pk, _depth=len(stack), _children=node['children'])
            )

    # Final flush of any remaining Prefixes
    prefix_model.objects.bulk_update(update_queue, ['_depth', '_children'])


def get_next_available_prefix(ipset, prefix_size):
    """
    Given a prefix length, allocate the next available prefix from an IPSet.
    """
    for available_prefix in ipset.iter_cidrs():
        if prefix_size >= available_prefix.prefixlen:
            allocated_prefix = f"{available_prefix.network}/{prefix_size}"
            ipset.remove(allocated_prefix)
            return allocated_prefix
    return None


#
# Address-space helpers (used by Prefix and IPRange address-counting paths)
#


def _to_ipaddress(value):
    """Normalize an IPAddressField/IPNetworkField value to a netaddr.IPAddress."""
    if isinstance(value, netaddr.IPAddress):
        return value
    if isinstance(value, netaddr.IPNetwork):
        return value.ip

    # Plain host strings (e.g. '192.0.2.1') avoid allocating an IPNetwork.
    # netaddr raises AddrFormatError for malformed input and ValueError for
    # otherwise-valid CIDR strings; both fall through to the IPNetwork path.
    text = str(value)
    try:
        return netaddr.IPAddress(text)
    except (netaddr.AddrFormatError, ValueError):
        return netaddr.IPNetwork(text).ip


def annotate_host_address(queryset):
    """Annotate an IPAddress queryset with its host address (mask ignored)."""
    return queryset.order_by().annotate(
        host_address=Cast(
            Host('address'),
            output_field=IPAddressField(),
        )
    )


def filter_ip_hosts_between(queryset, first_ip, last_ip):
    """Restrict an IPAddress queryset to hosts between first_ip and last_ip (mask ignored)."""
    return queryset.filter(
        address__host__inet__gte=first_ip,
        address__host__inet__lte=last_ip,
    )


def count_distinct_ip_hosts(queryset):
    """Count distinct host addresses in an IPAddress queryset (dedupes by host, not mask)."""
    return annotate_host_address(queryset).values('host_address').distinct().count()


def get_iprange_intervals(queryset, first_ip=None, last_ip=None):
    """Return IPRange rows as integer intervals, optionally clipped to bounds."""
    first_int = int(first_ip) if first_ip is not None else None
    last_int = int(last_ip) if last_ip is not None else None

    intervals = []

    # order_by() clears the model's default ordering; merge_ip_intervals sorts later anyway.
    for start_address, end_address in queryset.order_by().values_list(
        'start_address', 'end_address'
    ):
        start_int = int(_to_ipaddress(start_address))
        end_int = int(_to_ipaddress(end_address))

        if first_int is not None:
            if end_int < first_int:
                continue
            start_int = max(start_int, first_int)

        if last_int is not None:
            if start_int > last_int:
                continue
            end_int = min(end_int, last_int)

        intervals.append((start_int, end_int))

    return intervals


def merge_ip_intervals(intervals):
    """Return the union of integer IP intervals as a list of merged (start, end) tuples."""
    if not intervals:
        return []

    intervals = sorted(intervals)
    merged = []
    current_start, current_end = intervals[0]

    for start, end in intervals[1:]:
        if start <= current_end + 1:
            current_end = max(current_end, end)
            continue

        merged.append((current_start, current_end))
        current_start, current_end = start, end

    merged.append((current_start, current_end))
    return merged


def count_ip_intervals(intervals):
    """Count addresses covered by the supplied intervals. Callers should pass merged intervals."""
    return sum(end - start + 1 for start, end in intervals)


def count_distinct_ip_hosts_outside_intervals(queryset, intervals, version):
    """
    Count distinct host addresses not covered by any of the supplied integer intervals.

    Callers should pass merged intervals; overlapping inputs work correctly but bloat
    the SQL exclusion predicate.
    """
    queryset = annotate_host_address(queryset)

    if not intervals:
        return queryset.values('host_address').distinct().count()

    exclusion = Q()
    for start, end in intervals:
        exclusion |= Q(
            host_address__gte=netaddr.IPAddress(start, version=version),
            host_address__lte=netaddr.IPAddress(end, version=version),
        )

    return (
        queryset
        .exclude(exclusion)
        .values('host_address')
        .distinct()
        .count()
    )


def _iter_distinct_ip_host_intervals(queryset):
    """Yield distinct hosts from an IPAddress queryset as single-IP integer intervals, sorted."""
    hosts = (
        annotate_host_address(queryset)
        .values_list('host_address', flat=True)
        .distinct()
        .order_by('host_address')
        .iterator(chunk_size=5000)
    )

    for host in hosts:
        host_int = int(_to_ipaddress(host))
        yield host_int, host_int


def find_first_available_ip(first_ip, last_ip, ip_queryset, range_queryset=None):
    """Find the first available IP in [first_ip, last_ip] by streaming sorted hosts and merged ranges."""
    first_int = int(first_ip)
    last_int = int(last_ip)
    version = first_ip.version

    intervals = merge_ip_intervals(
        get_iprange_intervals(range_queryset, first_ip, last_ip)
    ) if range_queryset is not None else []

    # Fast path: one merged occupied interval covers the entire usable span.
    if intervals and intervals[0][0] <= first_int and intervals[0][1] >= last_int:
        return None

    candidate = first_int
    ip_queryset = filter_ip_hosts_between(ip_queryset, first_ip, last_ip)

    # Default tuple ordering compares (start, end) — ties on `start` are harmless
    # because the sweep handles overlapping intervals.
    for start, end in heapq.merge(
        intervals,
        _iter_distinct_ip_host_intervals(ip_queryset),
    ):
        if end < candidate:
            continue

        if start > candidate:
            return netaddr.IPAddress(candidate, version=version)

        candidate = max(candidate, end + 1)
        if candidate > last_int:
            return None

    if candidate <= last_int:
        return netaddr.IPAddress(candidate, version=version)

    return None
