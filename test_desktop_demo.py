"""Demo isolation and disposable sample data, independent of any installed service."""
import unittest
from functools import partial
from unittest.mock import patch

from desktop.demo import DemoClient
from management import ManagementError
from tools.build_packages import native_version


class DemoTests(unittest.TestCase):
    def login(self, demo):
        return demo.request('security.login', {'password': demo.request('status')['demo_data']['management_password']})['session']

    def confirm(self, demo, session, method, params):
        return demo.request('security.confirm', {'password': demo.request('status')['demo_data']['management_password'],
                            'method': method, 'params': params}, session=session)['confirmation']

    def test_random_samples_are_disposable_and_never_use_io(self):
        with patch('socket.socket', side_effect=AssertionError('network access')), \
             patch('subprocess.run', side_effect=AssertionError('system command')), \
             patch('subprocess.Popen', side_effect=AssertionError('process launch')), \
             patch('pathlib.Path.write_text', side_effect=AssertionError('configuration write')), \
             patch('pathlib.Path.write_bytes', side_effect=AssertionError('configuration write')):
            demo = DemoClient()
            before = demo.request('status')
            other = DemoClient().request('status')
            self.assertNotEqual(before['config']['hostname'], other['config']['hostname'])
            self.assertNotEqual(before['demo_data']['fields']['password'], other['demo_data']['fields']['password'])
            demo.request('ui.bootstrap')
            session = self.login(demo)
            request = partial(demo.request, session=session)
            request('onboarding.inspect')
            request('onboarding.prepare', {})
            request('onboarding.apply')
            request('settings.save', {'changes': {'hostname': 'changed.example.test'}})
            request('services.pause')
            request('preferences.save', {'changes': {'startup': 'manual'}})
            identity = request('agents.register', {}, confirmation=self.confirm(demo, session, 'agents.register', {}))
            self.assertNotIn('identity_path', request('ui.export_agent', {'identity': identity}))
            request('migration.start')
            after = demo.request('demo.reset')
            self.assertFalse(after['preferences']['paused'])
            self.assertEqual(after['preferences']['startup'], 'boot')
            self.assertNotEqual(after['config']['hostname'], 'changed.example.test')
            self.assertNotEqual(after['config']['hostname'], before['config']['hostname'])
            self.assertNotEqual(after['demo_data']['fields']['password'], before['demo_data']['fields']['password'])
            self.assertEqual(before['deployment']['phase'], 'idle')  # Responses do not alias mutable state.

    def test_simulated_credentials_and_service_controls(self):
        demo = DemoClient()
        request = partial(demo.request, session=self.login(demo))
        first = request('credentials.temporary')['temporary']
        self.assertNotEqual(first, request('credentials.regenerate')['temporary'])
        request('credentials.delete_passkey', {'id': 'demo-phone'})
        request('credentials.unlock')
        snapshot = demo.request('settings.read')['state']
        self.assertEqual(len(snapshot['passkey_list']), 1)
        self.assertEqual(snapshot['banned_ips'], 0)
        request('services.pause')
        request('services.restart')
        self.assertTrue(demo.request('status')['preferences']['paused'])
        request('services.resume')
        self.assertTrue(all(s['ActiveState'] == 'active' for s in demo.request('status')['services']))
        with self.assertRaises(ManagementError):
            request('unsupported.operation')

    def test_locked_session_cannot_modify_settings_or_read_secrets(self):
        demo = DemoClient()
        before = demo.request('status')
        protected = [('settings.save', {'changes': {'hostname': 'changed.example.test'}}),
                     ('preferences.save', {'changes': {'theme': 'dark'}}), ('services.pause', {}),
                     ('credentials.delete_passkey', {'id': 'demo-phone'}), ('credentials.regenerate', {}),
                     ('credentials.unlock', {}), ('settings.secrets', {}), ('credentials.temporary', {}),
                     ('agents.list', {}), ('agents.approve', {'request_id': 'demo-approval', 'allow': True}),
                     ('ui.export_agent', {}), ('onboarding.prepare', {}), ('migration.start', {})]
        session = self.login(demo)
        demo.request('security.logout', session=session)
        for token in (None, 'demo-session', session):
            for method, params in protected:
                with self.subTest(method=method, token=token), self.assertRaises(ManagementError) as caught:
                    demo.request(method, params, session=token)
                self.assertEqual(caught.exception.status, 401)
        self.assertEqual(demo.request('status'), before)
        self.assertEqual(demo.request('settings.read')['config'], before['config'])
        fresh = self.login(demo)
        self.assertNotEqual(fresh, session)
        demo.request('preferences.save', {'changes': {'theme': 'dark'}}, session=fresh)
        self.assertEqual(demo.request('status')['preferences']['theme'], 'dark')

    def test_passwords_and_dangerous_actions_require_real_confirmation(self):
        demo = DemoClient()
        for password in ('', 'incorrect-password'):
            with self.assertRaises(ManagementError) as caught:
                demo.request('security.login', {'password': password})
            self.assertEqual(caught.exception.status, 401)
        session = self.login(demo)
        params = {'name': '确认测试'}
        with self.assertRaises(ManagementError) as caught:
            demo.request('agents.register', params, session=session)
        self.assertEqual(caught.exception.status, 428)
        confirmation = self.confirm(demo, session, 'agents.register', params)
        demo.request('agents.register', params, session=session, confirmation=confirmation)
        with self.assertRaises(ManagementError):
            demo.request('agents.register', params, session=session, confirmation=confirmation)
        approval = {'request_id': 'demo-danger', 'allow': True, 'password': ''}
        with self.assertRaises(ManagementError):
            demo.request('agents.approve', approval, session=session)
        self.assertTrue(any(p['id'] == 'demo-danger' for p in demo.request('agents.list', session=session)['pending']))

    def test_password_change_and_reset_invalidate_sessions(self):
        demo = DemoClient()
        old_password = demo.request('status')['demo_data']['management_password']
        session = self.login(demo)
        new_password = {'password': 'New-demo-password-123!'}
        demo.request('security.password', new_password, session=session,
                     confirmation=self.confirm(demo, session, 'security.password', new_password))
        with self.assertRaises(ManagementError):
            demo.request('preferences.save', {'changes': {'theme': 'dark'}}, session=session)
        with self.assertRaises(ManagementError):
            demo.request('security.login', {'password': old_password})
        fresh = demo.request('security.login', new_password)['session']
        demo.request('demo.reset')
        with self.assertRaises(ManagementError):
            demo.request('preferences.save', {'changes': {'theme': 'dark'}}, session=fresh)

    def test_portal_uses_sample_data_and_blocks_network_and_form_posts(self):
        demo = DemoClient()
        for screen in ('password', 'temporary', 'passkey', 'success', 'credentials', 'credentials-passkey', 'error', 'ipv6'):
            body = demo.portal_preview(screen)
            self.assertIn("connect-src 'none'", body)
            self.assertIn("form-action 'none'", body)
            self.assertIn('demo-portal-data', body)
            self.assertNotIn('src="https://', body)

    def test_demo_registration_policy_and_current_passkey_permissions(self):
        demo = DemoClient()
        self.assertNotIn('id="register-key"', demo.portal_preview('credentials'))
        session = demo.request('security.login', {'password': demo.management_password})['session']
        demo.request('settings.save', {'changes': {'allow_passkey_registration': True}, 'revision': '1'}, session=session)
        body = demo.portal_preview('credentials')
        self.assertIn('id="register-key"', body)
        self.assertNotIn('action="/passkeys/delete"', body)
        body = demo.portal_preview('credentials-passkey')
        self.assertNotIn('id="register-key"', body)
        self.assertEqual(body.count('action="/passkeys/delete"'), 1)
        self.assertIn('name="key_id" value="demo-laptop"', body)
        self.assertNotIn('name="key_id" value="demo-phone"', body)
        with self.assertRaises(ManagementError):
            demo.request('settings.save', {'changes': {'allow_passkey_registration': 'true'}}, session=session)
        with self.assertRaises(ManagementError):
            demo.request('settings.save', {'changes': {'allow_passkey_registration': False}, 'revision': '1'}, session=session)
        demo.request('settings.save', {'changes': {'allow_passkey_registration': False}, 'revision': '2'}, session=session)
        self.assertNotIn('id="register-key"', demo.portal_preview('credentials'))

    def test_native_development_version(self):
        self.assertEqual(native_version('2.0.0-dev.1'), '2.0.0~dev.1')
        self.assertEqual(native_version('2.0.0'), '2.0.0')


if __name__ == '__main__':
    unittest.main()
