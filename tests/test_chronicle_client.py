# -*- coding: utf-8 -*-
"""The Scrobbler's ChronicleClient: request shape, the atomic settings snapshot, and never raising."""
import io
import json
import unittest
import urllib.error
from unittest import mock

import tests.kodi_stubs  # noqa: F401

from lib import chronicle_client


class _Resp:
    def __init__(self, body=b'{}', status=200):
        self._body, self.status = body, status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client(url='chronicle:7979/', key='k'):
    addon = mock.MagicMock()
    addon.getSetting.side_effect = lambda k: {'chronicle_url': url, 'api_key': key}.get(k, '')
    addon.getAddonInfo.return_value = 'service.chronicle.scrobbler'
    with mock.patch.object(chronicle_client, 'ADDON', addon):
        c = chronicle_client.ChronicleClient()
    return c, addon


class _Net(unittest.TestCase):
    def _urlopen(self, resp=None, exc=None):
        calls = []

        def fake(req, timeout=None):
            calls.append((req, timeout))
            if exc:
                raise exc
            return resp
        p = mock.patch.object(chronicle_client.urllib.request, 'urlopen', side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        return calls


class TestSettings(unittest.TestCase):
    def test_url_is_normalised_and_scheme_restricted(self):
        self.assertEqual(_client('chronicle:7979/')[0]._settings(), ('http://chronicle:7979', 'k'))
        self.assertEqual(_client('file:///etc/passwd')[0]._settings()[0], '')

    def test_refresh_picks_up_a_new_connection_as_one_snapshot(self):
        c, addon = _client('http://a', 'k1')
        addon.getSetting.side_effect = lambda k: {'chronicle_url': 'http://b', 'api_key': 'k2'}.get(k, '')
        self.assertEqual(c._settings(), ('http://a', 'k1'))
        with mock.patch.object(chronicle_client, 'ADDON', addon):
            c.refresh_settings()
        self.assertEqual(c._settings(), ('http://b', 'k2'))

    def test_unconfigured_client_does_no_network_and_returns_neutral_values(self):
        c, _ = _client('', '')
        with mock.patch.object(chronicle_client.urllib.request, 'urlopen') as urlopen:
            self.assertFalse(c.scrobble({'a': 1}))
            self.assertEqual(c.get_resume_state({'a': 1}), {})
            self.assertFalse(c.update_library_entry(1, user_rating=5))
            self.assertIsNone(c.get_lists())
            self.assertEqual(c.get_list(1), {})
            self.assertIsNone(c.get_current_user())
            self.assertEqual(c.test_connection()[0], False)
        urlopen.assert_not_called()


class TestScrobble(_Net):
    def test_sends_json_with_key_and_agent(self):
        calls = self._urlopen(_Resp(status=200))
        c, _ = _client()
        self.assertTrue(c.scrobble({'title': 'T', 'progressPercent': 5.0}))
        req = calls[0][0]
        self.assertEqual((req.full_url, req.get_method()), ('http://chronicle:7979/api/v1/scrobble', 'POST'))
        self.assertEqual(req.get_header('X-api-key'), 'k')
        self.assertEqual(req.get_header('User-agent'), 'Kodi/Chronicle-Scrobbler/1.0')
        self.assertEqual(json.loads(req.data), {'title': 'T', 'progressPercent': 5.0})

    def test_failures_are_false_not_exceptions(self):
        c, _ = _client()
        for exc in (urllib.error.HTTPError('u', 500, 'err', {}, io.BytesIO(b'')), urllib.error.URLError('x'), TimeoutError(), ValueError()):
            self._urlopen(exc=exc)
            self.assertFalse(c.scrobble({'title': 'T'}), exc)

    def test_unexpected_status_is_false(self):
        self._urlopen(_Resp(status=202))
        self.assertFalse(_client()[0].scrobble({'title': 'T'}))


class TestResume(_Net):
    def test_returns_data_dict(self):
        self._urlopen(_Resp(json.dumps({'data': {'resumePositionPercent': 12.5}}).encode()))
        self.assertEqual(_client()[0].get_resume_state({'title': 'T'}), {'resumePositionPercent': 12.5})

    def test_null_data_and_errors_are_the_empty_dict(self):
        c, _ = _client()
        self._urlopen(_Resp(b'{"data": null}'))
        self.assertEqual(c.get_resume_state({}), {})
        self._urlopen(exc=urllib.error.URLError('x'))
        self.assertEqual(c.get_resume_state({}), {})


class TestLists(_Net):
    def test_get_lists_distinguishes_empty_from_failed(self):
        c, _ = _client()
        self._urlopen(_Resp(b'{"data": []}'))
        self.assertEqual(c.get_lists(), [])
        self._urlopen(exc=urllib.error.URLError('x'))
        self.assertIsNone(c.get_lists())
        self._urlopen(_Resp(b'{"data": {"not": "a list"}}'))
        self.assertIsNone(c.get_lists())

    def test_get_list_returns_dict_or_empty(self):
        c, _ = _client()
        calls = self._urlopen(_Resp(b'{"data": {"id": 4, "items": []}}'))
        self.assertEqual(c.get_list(4)['id'], 4)
        self.assertTrue(calls[0][0].full_url.endswith('/api/v1/lists/4'))
        self._urlopen(exc=urllib.error.URLError('x'))
        self.assertEqual(c.get_list(4), {})


class TestLibraryAndHealth(_Net):
    def test_update_entry_sends_only_given_fields_and_nothing_when_empty(self):
        c, _ = _client()
        calls = self._urlopen(_Resp(status=204))
        self.assertTrue(c.update_library_entry(3, user_rating=8))
        self.assertEqual((calls[0][0].get_method(), json.loads(calls[0][0].data)), ('PATCH', {'userRating': 8}))
        self.assertTrue(c.update_library_entry(3))
        self.assertEqual(len(calls), 1)

    def test_test_connection_reports_http_errors(self):
        c, _ = _client()
        self._urlopen(_Resp(status=200))
        self.assertEqual(c.test_connection(), (True, ''))
        self._urlopen(exc=urllib.error.HTTPError('u', 401, 'Unauthorized', {}, io.BytesIO(b'')))
        self.assertEqual(c.test_connection(), (False, 'HTTP 401: Unauthorized'))
        self._urlopen(exc=urllib.error.URLError('refused'))
        ok, message = c.test_connection()
        self.assertFalse(ok)
        self.assertIn('refused', message)

    def test_get_current_user_uses_a_short_timeout(self):
        calls = self._urlopen(_Resp(b'{"data": {"username": "u"}}'))
        self.assertEqual(_client()[0].get_current_user(), {'username': 'u'})
        self.assertEqual(calls[0][1], 5)


class TestSharedUrl(unittest.TestCase):
    def test_delegates_with_this_addons_id(self):
        addon = mock.MagicMock()
        addon.getAddonInfo.return_value = 'service.chronicle.scrobbler'
        with mock.patch.object(chronicle_client, 'ADDON', addon), \
                mock.patch.object(chronicle_client, '_find_shared_url', return_value='http://x') as find:
            self.assertEqual(chronicle_client.find_shared_chronicle_url(), 'http://x')
        self.assertEqual(find.call_args[0][0], 'service.chronicle.scrobbler')


if __name__ == '__main__':
    unittest.main()
