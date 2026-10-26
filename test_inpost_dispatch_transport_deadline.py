"""No-network checks at the final ShipX dispatch boundary."""
from datetime import datetime, time
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import inpost_module as transport
import inpost_pickups as pickups


class DispatchTransportDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 26, 12, 20, 58, tzinfo=ZoneInfo('Europe/Warsaw'))
        self.pickup = {
            'name': 'Magazyn', 'street': 'Testowa 1', 'city': 'Warszawa',
            'post_code': '00-001', 'phone': '500600700',
        }
        self.enterContext(patch.object(pickups, '_local_now', lambda: self.now))
        self.enterContext(patch.object(pickups, 'cutoff_time', lambda: time(12, 20)))

    def test_slow_organization_discovery_never_posts_after_deadline(self):
        for daily in (False, True):
            with self.subTest(daily=daily):
                self.now = self.now.replace(hour=12, minute=20, second=58)
                pickup = dict(self.pickup)
                if daily:
                    pickup[pickups.DAILY_META] = {'scheduled_date': '2026-10-26'}
                calls = []

                def fake_request(path, method='GET', payload=None, **kwargs):
                    calls.append((method, path))
                    if path == '/organizations':
                        self.now = self.now.replace(minute=21, second=5)
                        return {'items': [{'id': 7, 'services': ['inpost_courier_standard']}]}
                    return {'id': 900, 'status': 'sent'}

                with patch.object(transport, 'config_summary', lambda: {'organization_id': ''}), \
                        patch.object(transport, '_DISCOVERED_ORGANIZATION_ID', ''), \
                        patch.object(transport, '_request', fake_request):
                    with self.assertRaisesRegex(transport.InPostError, 'termin'):
                        transport.create_dispatch_order(['41'], pickup)
                self.assertEqual(calls, [('GET', '/organizations')])

    def test_manual_request_before_cutoff_keeps_existing_payload(self):
        self.now = self.now.replace(hour=10, minute=0, second=0)
        request = Mock(return_value={'id': 900, 'status': 'sent'})
        with patch.object(transport, 'organization_id', lambda: '7'), \
                patch.object(transport, '_request', request):
            result = transport.create_dispatch_order(['41'], self.pickup)
        self.assertEqual(result['id'], 900)
        args = request.call_args.args
        self.assertEqual(args[:2], ('/organizations/7/dispatch_orders', 'POST'))
        self.assertEqual(args[2]['shipments'], ['41'])
        self.assertNotIn(pickups.DAILY_META, args[2])

    def test_last_allowed_second_can_post(self):
        self.now = self.now.replace(second=59, microsecond=999999)
        request = Mock(return_value={'id': 900})
        with patch.object(transport, 'organization_id', lambda: '7'), \
                patch.object(transport, '_request', request):
            transport.create_dispatch_order(['41'], self.pickup)
        request.assert_called_once()

    def test_wrong_scheduled_day_or_weekend_cannot_post(self):
        for day, when in (
                ('2026-10-27', self.now),
                ('2026-10-23', self.now),
                ('2026-10-31', self.now.replace(day=31))):
            with self.subTest(day=day, when=when):
                self.now = when
                pickup = dict(self.pickup, **{pickups.DAILY_META: {'scheduled_date': day}})
                request = Mock()
                with patch.object(transport, 'organization_id', lambda: '7'), \
                        patch.object(transport, '_request', request):
                    with self.assertRaises(transport.InPostError):
                        transport.create_dispatch_order(['41'], pickup)
                request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
