from django.contrib.auth.models import AnonymousUser
from django.template import Context, Template
from django.test import RequestFactory, TestCase

from dcim.models import Device, Site
from dcim.tables import DeviceTable
from netbox.tables import NetBoxTable, columns
from utilities.paginator import EnhancedPaginator
from utilities.testing import create_paginated_test_table, create_tags, create_test_device, create_test_user


class BaseTableTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        create_test_device('Test Device 1')
        cls.user = create_test_user('testuser')

    def test_prefetch_visible_columns(self):
        """
        Verify that the table queryset's prefetch_related lookups correspond to the user's
        visible column preferences. Columns referencing related fields should only be
        prefetched when those columns are visible.
        """
        request = RequestFactory().get('/')
        request.user = self.user

        # Scenario 1: 'rack' (simple FK) and 'region' (nested accessor: site__region) are visible
        self.user.config.set(
            'tables.DeviceTable.columns',
            ['name', 'status', 'site', 'rack', 'region'],
            commit=True,
        )
        table = DeviceTable(Device.objects.all())
        table.configure(request)
        prefetch_lookups = table.data.data._prefetch_related_lookups
        self.assertIn('rack', prefetch_lookups)
        self.assertIn('site__region', prefetch_lookups)

        # Scenario 2: Local fields only; no prefetching
        self.user.config.set(
            'tables.DeviceTable.columns',
            ['name', 'status', 'description'],
            commit=True,
        )
        table = DeviceTable(Device.objects.all())
        table.configure(request)
        prefetch_lookups = table.data.data._prefetch_related_lookups
        self.assertEqual(prefetch_lookups, tuple())

    def test_prefetch_all_columns_for_export(self):
        """
        Verify that related fields for *all* table columns are prefetched when preparing a CSV
        export, including columns which are not currently visible in the user's configured view.
        """
        request = RequestFactory().get('/')
        request.user = self.user

        # Configure the table with only local-field columns visible. Related columns like 'site',
        # 'rack', and 'region' are hidden in the user's view.
        self.user.config.set(
            'tables.DeviceTable.columns',
            ['name', 'status'],
            commit=True,
        )
        table = DeviceTable(Device.objects.all())
        table.configure(request)

        # With only local-field columns visible, no relations should be prefetched yet.
        self.assertEqual(table.data.data._prefetch_related_lookups, tuple())

        # Simulate the CSV "All data" export path: re-apply prefetching for every column that
        # will be included in the export, regardless of visibility.
        export_columns = [
            col_name for col_name, _ in table.selected_columns + table.available_columns
        ]
        table._apply_prefetching(columns=export_columns)

        prefetch_lookups = table.data.data._prefetch_related_lookups
        self.assertIn('rack', prefetch_lookups)
        self.assertIn('site__region', prefetch_lookups)

    def test_configure_anonymous_user_with_ordering(self):
        """
        Verify that table.configure() does not raise an error when an anonymous
        user sorts a table column.
        """
        request = RequestFactory().get('/?sort=name')
        request.user = AnonymousUser()
        table = DeviceTable(Device.objects.all())
        table.configure(request)


class TagColumnTable(NetBoxTable):
    tags = columns.TagColumn(url_name='dcim:site_list')

    class Meta(NetBoxTable.Meta):
        model = Site
        fields = ('pk', 'name', 'tags',)
        default_columns = fields


class TagColumnTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        tags = create_tags('Alpha', 'Bravo', 'Charlie')

        sites = [
            Site(name=f'Site {i}', slug=f'site-{i}') for i in range(1, 6)
        ]
        Site.objects.bulk_create(sites)
        for site in sites:
            site.tags.add(*tags)

    def test_tagcolumn(self):
        template = Template('{% load render_table from django_tables2 %}{% render_table table %}')
        table = TagColumnTable(Site.objects.all(), orderable=False)
        context = Context({
            'table': table
        })
        template.render(context)


class PaginatorTemplateTestCase(TestCase):
    """
    Verify that `inc/paginator.html` emits prefix-aware query parameters when a
    table is supplied, and falls back to bare `page` / `per_page` when no table
    is in context (e.g. rack_elevation_list, custom field choice set choices).
    """

    PAGINATOR_TEMPLATE = (
        "{% include 'inc/paginator.html' with "
        "table=table paginator=paginator page=page %}"
    )
    NO_TABLE_PAGINATOR_TEMPLATE = (
        "{% include 'inc/paginator.html' with "
        "paginator=paginator page=page %}"
    )
    PAGE_ONLY_PAGINATOR_TEMPLATE = (
        "{% include 'inc/paginator.html' with page=page %}"
    )

    def test_prefixed_paginator_uses_prefixed_query_params(self):
        """Prefixed tables emit prefixed `page=` and `per_page=` query parameters."""
        table, request = create_paginated_test_table(prefix='log-')

        html = Template(self.PAGINATOR_TEMPLATE).render(Context({
            'table': table,
            'paginator': table.paginator,
            'page': table.page,
            'request': request,
        }))

        self.assertIn('log-page=', html)
        self.assertNotRegex(html, r'[?&]page=\d')
        self.assertIn('log-per_page=', html)
        self.assertNotRegex(html, r'[?&]per_page=\d')

    def test_unprefixed_paginator_uses_bare_query_params(self):
        """Unprefixed tables continue to emit bare `page=` query parameters."""
        table, request = create_paginated_test_table()

        html = Template(self.PAGINATOR_TEMPLATE).render(Context({
            'table': table,
            'paginator': table.paginator,
            'page': table.page,
            'request': request,
        }))

        self.assertRegex(html, r'[?&]page=\d')
        self.assertNotIn('log-page=', html)

    def test_paginator_without_table_uses_bare_query_params(self):
        """Callers that include the partial without a `table` still get bare `page=` links."""
        request = RequestFactory().get('/')
        request.user = AnonymousUser()
        data = list(range(120))
        paginator = EnhancedPaginator(data, per_page=50)
        page = paginator.page(1)

        html = Template(self.NO_TABLE_PAGINATOR_TEMPLATE).render(Context({
            'paginator': paginator,
            'page': page,
            'request': request,
        }))

        self.assertRegex(html, r'[?&]page=\d')
        self.assertRegex(html, r'[?&]per_page=\d')

    def test_paginator_with_only_page_kwarg(self):
        """Match `customfieldchoiceset_choices.html`: only `page` is provided."""
        request = RequestFactory().get('/')
        request.user = AnonymousUser()
        paginator = EnhancedPaginator(list(range(120)), per_page=50)

        html = Template(self.PAGE_ONLY_PAGINATOR_TEMPLATE).render(Context({
            'page': paginator.page(1),
            'request': request,
        }))

        self.assertRegex(html, r'[?&]per_page=\d')
