import tempfile
import unittest
from pathlib import Path

from desktop.security import Authority
from desktop.services import Services
from management import ManagementError


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 10000
        self.peer = {'uid': 1000, 'pid': 42}
        self.other = {'uid': 1000, 'pid': 99}
        self.password = 'Management-password-123!'
        self.auth = Authority(Path(self.tmp.name), lambda: self.now)
        self.auth.initialize(1000, self.password)
        self.session = self.auth.login(self.peer, self.password)
        self.identity = self.auth.register('我的 Agent')

    def test_desktop_session_cannot_be_reused_by_another_process(self):
        self.auth.authorize(self.peer, 'settings.save', {}, session=self.session)
        with self.assertRaises(ManagementError):
            self.auth.authorize(self.other, 'settings.save', {}, session=self.session)
        self.auth.logout(self.peer, self.session)
        with self.assertRaises(ManagementError):
            self.auth.authorize(self.peer, 'settings.save', {}, session=self.session)

    def test_agent_grant_fixed_hour_crosses_tasks_but_not_agent_identity(self):
        request = self.auth.request_grant(self.other, self.identity)
        self.auth.approve(self.peer, self.session, request['request_id'], True)
        self.auth.authorize({'uid': 1000, 'pid': 100}, 'settings.save', {}, identity=self.identity)
        second = self.auth.register('另一个 Agent')
        with self.assertRaises(ManagementError):
            self.auth.authorize(self.other, 'settings.save', {}, identity=second)
        self.now += 3599
        self.auth.authorize(self.other, 'services.pause', {}, identity=self.identity)
        self.now += 1
        with self.assertRaises(ManagementError):
            self.auth.authorize(self.other, 'services.pause', {}, identity=self.identity)

    def test_grant_cannot_bypass_dangerous_confirmation(self):
        request = self.auth.request_grant(self.other, self.identity)
        self.auth.approve(self.peer, self.session, request['request_id'], True)
        params = {'password': 'Replacement-password-123!'}
        with self.assertRaises(ManagementError):
            self.auth.authorize(self.other, 'security.password', params, identity=self.identity)
        pending = next(x for x in self.auth.summary()['pending'] if x['kind'] == 'dangerous')
        with self.assertRaises(ManagementError):
            self.auth.approve(self.peer, self.session, pending['id'], True)
        self.auth.approve(self.peer, self.session, pending['id'], True, self.password)
        self.auth.authorize(self.other, 'security.password', params, identity=self.identity)
        with self.assertRaises(ManagementError):
            self.auth.authorize(self.other, 'security.password', params, identity=self.identity)

    def test_human_confirmation_is_exact_and_one_use(self):
        params = {'name': 'trusted'}
        token = self.auth.confirm(self.peer, self.session, self.password, 'agents.register', params)
        with self.assertRaises(ManagementError):
            self.auth.authorize(self.peer, 'agents.register', {'name': 'changed'}, self.session, confirmation=token)
        token = self.auth.confirm(self.peer, self.session, self.password, 'agents.register', params)
        self.auth.authorize(self.peer, 'agents.register', params, self.session, confirmation=token)
        with self.assertRaises(ManagementError):
            self.auth.authorize(self.peer, 'agents.register', params, self.session, confirmation=token)

    def test_revoke_and_password_change_invalidate_grants(self):
        request = self.auth.request_grant(self.other, self.identity)
        self.auth.approve(self.peer, self.session, request['request_id'], True)
        self.auth.revoke(self.identity['id'])
        with self.assertRaises(ManagementError):
            self.auth.authorize(self.other, 'settings.save', {}, identity=self.identity)
        self.auth.change_password('Changed-management-123!')
        with self.assertRaises(ManagementError):
            self.auth.human(self.peer, self.session)
        text = self.auth.path.read_text()
        self.assertNotIn(self.password, text)
        self.assertNotIn(self.identity['key'], text)

    def test_failed_passwords_rate_limited_and_other_uid_rejected(self):
        for _ in range(5):
            with self.assertRaises(ManagementError):
                self.auth.login(self.peer, 'wrong')
        with self.assertRaises(ManagementError) as error:
            self.auth.login(self.peer, self.password)
        self.assertEqual(error.exception.status, 429)
        self.now += 300
        self.auth.login(self.peer, self.password)
        with self.assertRaises(ManagementError):
            self.auth.login({'uid': 1001, 'pid': 42}, self.password)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config = self.root / 'etc/rdp-access-auth/portal-settings.json'
        config.parent.mkdir(parents=True)
        config.write_text('{}')
        self.calls = []
        self.services = Services(self.root, lambda args: self.calls.append(args) or '')

    def test_pause_survives_new_controller_and_settings_changes(self):
        self.services.action('pause')
        restarted = Services(self.root, lambda args: self.calls.append(args) or '')
        self.assertTrue(restarted.prefs['paused'])
        self.assertTrue(restarted.pause_marker.exists())
        restarted.preferences({'startup': 'boot'})
        self.assertEqual(self.calls[-1][1], 'disable')
        with self.assertRaises(ManagementError):
            restarted.action('restart')
        restarted.action('resume')
        self.assertFalse(restarted.pause_marker.exists())
        self.assertFalse(restarted.prefs['paused'])

    def test_exit_checkbox_controls_next_boot_without_changing_pause(self):
        self.services.action('exit', disable_autostart=True)
        self.assertEqual(self.services.prefs['startup'], 'manual')
        self.assertEqual(self.calls[-1][1], 'stop')
        self.services.action('exit')
        self.assertEqual(self.services.prefs['startup'], 'boot')
        self.services.action('pause')
        self.services.action('exit')
        self.assertTrue(self.services.prefs['paused'])

    def test_login_never_resumes_manual_pause(self):
        self.services.preferences({'startup': 'login'})
        self.services.action('pause')
        self.calls.clear()
        self.services.action('login')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
