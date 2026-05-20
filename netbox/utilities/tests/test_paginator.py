from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase

from utilities.paginator import get_paginate_count


class GetPaginateCountTestCase(TestCase):
    @staticmethod
    def _request(path):
        request = RequestFactory().get(path)
        request.user = AnonymousUser()
        return request

    def test_bare_per_page_default(self):
        """Default behavior reads the bare `per_page` query parameter."""
        request = self._request('/dummy/?per_page=37')
        self.assertEqual(get_paginate_count(request), 37)

    def test_prefixed_per_page_argument(self):
        """When passed a custom field name, only that field is consulted."""
        request = self._request('/dummy/?log-per_page=37&per_page=999')
        self.assertEqual(
            get_paginate_count(request, per_page_field='log-per_page'),
            37,
        )

    def test_prefixed_field_ignores_bare_per_page(self):
        """A prefixed lookup does not bleed in unrelated bare `per_page` values."""
        request = self._request('/dummy/?per_page=999')
        result = get_paginate_count(request, per_page_field='log-per_page')
        self.assertNotEqual(result, 999)
