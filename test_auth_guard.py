from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from unittest.mock import patch
from auth_guard import AuthGuard

class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name + '/state.sqlite3'
        self.guard = AuthGuard(self.path)
        self.clock = patch('auth_guard.time.time', return_value=10000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)

    def fail(self, ip):
        token, wait, _ = self.guard.begin(ip)
        self.assertIsNotNone(token)
        self.assertEqual(wait, 0)
        self.guard.finish(token, False)

    def ban(self, ip):
        for _ in range(5):
            self.fail(ip)

    def test_five_failures_ban_ip_and_persist_after_restart(self):
        self.ban('1.1.1.1')
        self.assertEqual(AuthGuard(self.path).begin('1.1.1.1')[1:], (900, 'ip'))
        self.assertIsNotNone(self.guard.begin('8.8.8.8')[0])
        self.now.return_value += 900
        self.fail('1.1.1.1')
        self.assertEqual(self.guard.locked('1.1.1.1'), (0, ''))

    def test_success_resets_consecutive_failures(self):
        for _ in range(4):
            self.fail('1.1.1.1')
        token, _, _ = self.guard.begin('1.1.1.1')
        self.guard.finish(token, True)
        for _ in range(4):
            self.fail('1.1.1.1')
        self.assertEqual(self.guard.locked('1.1.1.1'), (0, ''))

    def test_five_distinct_bans_lock_global_then_expire(self):
        for i in range(5):
            self.ban(f'1.1.1.{i+1}')
        self.assertEqual(AuthGuard(self.path).begin('8.8.8.8')[1:], (900, 'global'))
        self.now.return_value += 900
        self.assertIsNotNone(self.guard.begin('8.8.8.8')[0])

    def test_old_bans_do_not_trigger_global_lock(self):
        for i in range(4):
            self.ban(f'1.1.1.{i+1}')
        self.now.return_value += 601
        self.ban('8.8.8.8')
        self.assertEqual(self.guard.locked('9.9.9.9'), (0, ''))

    def test_parallel_requests_cannot_exceed_five_checks(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: self.guard.begin('1.1.1.1'), range(12)))
        tokens = [token for token, _, _ in results if token]
        self.assertEqual(len(tokens), 5)
        for token in tokens:
            self.guard.finish(token, False)
        self.assertEqual(self.guard.locked('1.1.1.1'), (900, 'ip'))

    def test_transient_failures_and_duplicate_completion_do_not_count(self):
        for _ in range(6):
            token, _, _ = self.guard.begin('1.1.1.1')
            self.guard.finish(token, None)
            self.guard.finish(token, False)
        self.assertEqual(self.guard.locked('1.1.1.1'), (0, ''))

    def test_crashed_inflight_request_expires(self):
        for _ in range(5):
            self.assertIsNotNone(self.guard.begin('1.1.1.1')[0])
        self.assertEqual(self.guard.begin('1.1.1.1')[2], 'pending')
        self.now.return_value += 60
        self.assertIsNotNone(self.guard.begin('1.1.1.1')[0])

    def test_challenge_requests_throttled_without_banning_password_login(self):
        for _ in range(30):
            self.assertTrue(self.guard.options_allowed('1.1.1.1'))
        self.assertFalse(self.guard.options_allowed('1.1.1.1'))
        self.assertIsNotNone(self.guard.begin('1.1.1.1')[0])
        self.now.return_value += 300
        self.assertTrue(self.guard.options_allowed('1.1.1.1'))

if __name__ == '__main__':
    unittest.main()
