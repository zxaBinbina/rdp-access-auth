"""Check Turnstile at the real authentication handlers, without external services."""
import io
import json
import unittest
from unittest.mock import patch, MagicMock
from urllib.parse import parse_qs
import test_portal
from portal import create_app

class TurnstileTests(unittest.TestCase):
    setUp = test_portal.PortalTests.setUp
    csrf = test_portal.PortalTests.csrf
    send = test_portal.PortalTests.send
    api = test_portal.PortalTests.api
    make_credential = test_portal.PortalTests.make_credential

    def enable(self):
        self.settings.update(turnstile_site_key='test-sitekey',turnstile_secret_key='test-server-secret')
        self.app=create_app(self.settings,self.state,self.grants.append)
        self.client=self.app.test_client()

    def result(self, action='login_password', **overrides):
        response=io.BytesIO(json.dumps({'success':True,'hostname':self.settings['hostname'],'action':action,**overrides}).encode())
        response.status=200
        opener=MagicMock();opener.open.return_value=response
        return patch('portal.urllib.request.build_opener',return_value=opener),opener

    def test_missing_tokens_no_network_no_rotation_no_lockout(self):
        self.enable()
        with patch('portal.urllib.request.build_opener') as network:
            for method in ('password','temporary'):
                for token in ('',' ','x'*2049):
                    self.assertEqual(self.send(method,temporary=self.initial,**{'cf-turnstile-response':token}).status_code,403)
            self.assertEqual(self.api('auth/verify',credential={}).status_code,403)
            self.assertEqual(self.api('auth/verify',**{'cf-turnstile-response':123}).status_code,403)
            network.assert_not_called()
        self.assertEqual(self.grants,[]);self.assertEqual(self.temp.current(),self.initial)
        self.assertEqual(self.app.extensions['auth_guard'].locked('1.1.1.1')[0],0)

    def test_success_trusted_ip_and_server_side_secret(self):
        self.enable();mock,opener=self.result()
        with mock:self.assertEqual(self.send(**{'cf-turnstile-response':'fresh-token'}).status_code,200)
        req=opener.open.call_args.args[0]
        self.assertEqual(req.full_url,'https://challenges.cloudflare.com/turnstile/v0/siteverify')
        self.assertEqual(parse_qs(req.data.decode()),{'secret':['test-server-secret'],'response':['fresh-token'],'remoteip':['1.1.1.1']})
        self.assertEqual(opener.open.call_args.kwargs['timeout'],10)
        self.assertEqual(self.grants,['1.1.1.1'])

    def test_hostname_action_success_and_replay_rejected(self):
        self.enable()
        for overrides in ({'hostname':'localhost'},{'hostname':'evil.example'},{'action':'login_temporary'},{'success':1},{'success':False,'error-codes':['timeout-or-duplicate']}):
            mock,_=self.result(**overrides)
            with mock:self.assertEqual(self.send(**{'cf-turnstile-response':'invalid'}).status_code,403)
        self.assertEqual(self.grants,[])

    def test_service_errors_fail_closed_without_penalty(self):
        self.enable()
        for _ in range(6):
            with patch('portal.urllib.request.build_opener',side_effect=TimeoutError):
                self.assertEqual(self.send('temporary',temporary=self.initial,**{'cf-turnstile-response':'token'}).status_code,503)
        self.assertEqual(self.temp.current(),self.initial);self.assertEqual(self.grants,[])
        self.assertEqual(self.app.extensions['auth_guard'].locked('1.1.1.1')[0],0)
        for body,status in ((b'not json',200),(b'[]',200),(b'{}',500)):
            response=io.BytesIO(body);response.status=status
            with patch('portal.urllib.request.build_opener') as p:
                p.return_value.open.return_value=response
                self.assertEqual(self.send(**{'cf-turnstile-response':'token'}).status_code,503)

    def test_temporary_rotation(self):
        self.enable();mock,_=self.result(action='login_temporary')
        with mock:self.assertEqual(self.send('temporary',temporary=self.initial,**{'cf-turnstile-response':'fresh'}).status_code,200)
        self.assertNotEqual(self.temp.current(),self.initial)

    def test_does_not_bypass_password(self):
        self.enable();mock,_=self.result()
        with mock:self.assertEqual(self.send(password='wrong',**{'cf-turnstile-response':'fresh'}).status_code,401)
        self.assertEqual(self.grants,[])

    def test_passkey_requires_both_checks(self):
        test_portal.PortalTests.enroll(self)
        self.enable();o=self.api('auth/options').json;credential=self.make_credential(o)
        self.assertEqual(self.api('auth/verify',challenge_id=o['challenge_id'],credential=credential).status_code,403)
        mock,_=self.result(action='login_passkey')
        with mock:self.assertEqual(self.api('auth/verify',challenge_id=o['challenge_id'],credential=credential,**{'cf-turnstile-response':'fresh'}).status_code,200)
        self.assertEqual(len(self.grants),2)

    def test_page_csp_actions_no_secret(self):
        self.enable()
        for method in ('password','temporary','passkey'):
            r=self.client.get('/?method='+method,base_url=self.base,headers=self.headers)
            self.assertIn('name="method" value="'+method+'"',r.text)
            self.assertIn("action: 'login_' + document.getElementById('auth-form').elements.method.value",r.text)
            self.assertIn('frame-src https://challenges.cloudflare.com',r.headers['Content-Security-Policy'])
            self.assertIn('test-sitekey',r.text);self.assertNotIn('test-server-secret',r.text)

    def test_partial_config_rejected(self):
        for setting in ('turnstile_site_key','turnstile_secret_key'):
            with self.assertRaises(ValueError):create_app({**self.settings,setting:'nonempty'},self.state)

if __name__=='__main__':unittest.main()
