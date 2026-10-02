from concurrent.futures import ThreadPoolExecutor
import hashlib, json, re, secrets, tempfile, threading, time, unittest
from pathlib import Path
from unittest.mock import patch
import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from auth_credentials import b64, password_hash
from portal import create_app

class PortalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.state=self.tmp.name+'/state.sqlite3';self.base='https://auth.example.test'
        wordlist=Path(self.tmp.name)/'words.json'
        wordlist.write_text(json.dumps(['测试'+chr(0x4e00+i) for i in range(2048)]))
        self.settings=dict(hostname='auth.example.test',rdp_address='desktop.example.test:26869',session_key='test-secret',
            password_salt='aa'*16,password_hash=password_hash('Correct-password-1234','aa'*16),sakura_token='unused',tunnel_id=1,
            wordlist_path=str(wordlist))
        self.grants=[];self.app=create_app(self.settings,self.state,self.grants.append);self.client=self.app.test_client()
        self.headers={'CF-Connecting-IP':'1.1.1.1','Origin':self.base}
        self.temp=self.app.extensions['temporary_passwords'];self.keys=self.app.extensions['passkeys'];self.initial=self.temp.current()
    def csrf(self,c=None):
        r=(c or self.client).get('/',base_url=self.base,headers=self.headers)
        self.assertEqual(r.status_code,200);return re.search(r'name="csrf" value="([^"]+)"',r.text).group(1)
    def send(self,method='password',c=None,**kw):
        c=c or self.client
        return c.post('/authorize',base_url=self.base,headers=self.headers,data={'csrf':self.csrf(c),'method':method,'password':'Correct-password-1234',**kw})
    def api(self,path,**kw):
        return self.client.post('/passkeys/'+path,base_url=self.base,headers=self.headers,json={'csrf':self.csrf(),**kw})
    def test_fixed_repeatable_no_rotation(self):
        for _ in range(2):self.assertEqual(self.send().status_code,200)
        self.assertEqual(self.temp.current(),self.initial);self.assertEqual(len(self.grants),2)
    def test_removed_methods(self):
        for m in ('totp','recovery','device','passkey'):self.assertEqual(self.send(m).status_code,400)
        self.assertEqual(self.grants,[])
    def test_temporary_rotation_and_next_display(self):
        r=self.send('temporary',temporary=self.initial,password='');self.assertEqual(r.status_code,200)
        new=self.temp.current();self.assertNotEqual(new,self.initial);self.assertIn(new,r.text);self.assertEqual(len(new.split('-')),3)
        self.assertEqual(self.send('temporary',temporary=self.initial).status_code,401)
        self.assertEqual(self.send('temporary',temporary=new.replace('-',' ')).status_code,200)
    def test_three_temporary_words_rotate(self):
        words = self.initial.split('-')
        response = self.send('temporary', **{f'temporary_{i+1}': word for i, word in enumerate(words)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.grants), 1)
        self.assertNotEqual(self.temp.current(), self.initial)

    def test_incomplete_temporary_words_do_not_authorize(self):
        response = self.send('temporary', temporary_1=self.initial.split('-')[0], temporary=self.initial)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.grants, [])
        self.assertEqual(self.temp.current(), self.initial)

    def test_restart_retains_rotation_and_encryption(self):
        self.assertNotIn(self.initial.encode(),Path(self.state).read_bytes());self.send('temporary',temporary=self.initial)
        app=create_app(self.settings,self.state,self.grants.append)
        self.assertEqual(app.extensions['temporary_passwords'].current(),self.temp.current())
        self.assertEqual(self.send('temporary',c=app.test_client(),temporary=self.initial).status_code,401)
        self.assertNotIn(self.temp.current().encode(),Path(self.state).read_bytes())
    def test_invalid_auth_csrf_and_ipv4_never_rotate(self):
        for kw,status in [({'temporary':'wrong'},401),({'temporary':self.initial,'csrf':'伪造'},403),({'temporary':self.initial,'ipv4':'192.168.1.1'},400)]:
            self.assertEqual(self.send('temporary',**kw).status_code,status)
        self.assertEqual(self.temp.current(),self.initial);self.assertEqual(self.grants,[])
    def test_upstream_failure_no_rotation_or_credential_penalty(self):
        def fail(ip):raise RuntimeError('test upstream failure')
        app=create_app(self.settings,self.state,fail)
        for _ in range(6):self.assertEqual(self.send('temporary',c=app.test_client(),temporary=self.initial).status_code,502)
        self.assertEqual(self.temp.current(),self.initial);self.assertEqual(self.send('temporary',temporary=self.initial).status_code,200)
    def test_concurrent_temporary_has_one_winner(self):
        barrier=threading.Barrier(3)
        def run(_):
            c=self.app.test_client();csrf=self.csrf(c);barrier.wait(timeout=5)
            return c.post('/authorize',base_url=self.base,headers=self.headers,data={'csrf':csrf,'method':'temporary','temporary':self.initial}).status_code
        with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(run,range(3)))
        self.assertEqual(results.count(200),1);self.assertTrue(all(r in (200,401,409) for r in results));self.assertEqual(len(self.grants),1)
    def test_five_failures_ban_every_method_across_restart(self):
        for _ in range(4):self.assertEqual(self.send(password='wrong').status_code,401)
        self.assertEqual(self.send('temporary',temporary='wrong').status_code,429)
        self.assertEqual(self.send().status_code,429);self.assertEqual(self.api('auth/options').status_code,429)
        self.assertEqual(self.send(c=create_app(self.settings,self.state,self.grants.append).test_client()).status_code,429)
    def test_five_distinct_bans_lock_global(self):
        for i in range(5):
            self.headers['CF-Connecting-IP']=f'1.1.1.{i+1}'
            for _ in range(5):self.send(password='wrong')
        self.headers['CF-Connecting-IP']='8.8.8.8';r=self.send()
        self.assertEqual(r.status_code,429);self.assertIn('系统已临时锁定',r.text);self.assertIn('Retry-After',r.headers);self.assertEqual(self.grants,[])
    def test_origin_host_connector_cookie_size_guards(self):
        self.assertEqual(self.client.get('/',base_url=self.base).status_code,403)
        self.assertEqual(self.client.get('/',base_url='https://evil.example',headers=self.headers).status_code,400)
        self.assertEqual(self.client.get('/',base_url=self.base,headers=self.headers,environ_overrides={'REMOTE_ADDR':'8.8.8.8'}).status_code,403)
        csrf=self.csrf()
        for origin in ('https://evil.example',self.base+':444',self.base+'/evil'):
            self.assertEqual(self.client.post('/authorize',base_url=self.base,headers={**self.headers,'Origin':origin},data={'csrf':csrf}).status_code,403)
        r=self.client.get('/',base_url=self.base,headers=self.headers)
        for a in ('Secure','HttpOnly','SameSite=Strict'):self.assertIn(a,r.headers['Set-Cookie'])
        self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy']);self.assertEqual(self.send(password='a'*70000).status_code,413)
    def test_privacy_origin_multitab_network_change(self):
        self.assertEqual(self.csrf(),self.csrf());self.headers.update(Origin='null',**{'CF-Connecting-IP':'8.8.8.8'})
        self.assertEqual(self.send().status_code,200);self.assertEqual(self.grants,['8.8.8.8'])
    def test_ipv6_only_grants_public_ipv4(self):
        self.headers['CF-Connecting-IP']='2606:4700:4700::1111'
        self.assertEqual(self.send('temporary',temporary=self.initial).status_code,400);self.assertEqual(self.temp.current(),self.initial)
        self.assertEqual(self.send('temporary',temporary=self.initial,ipv4='8.8.8.8').status_code,200);self.assertEqual(self.grants,['8.8.8.8'])
    def test_management_requires_fresh_authentication(self):
        self.assertEqual(self.client.get('/credentials',base_url=self.base,headers=self.headers).status_code,403)
        self.assertEqual(self.api('register/options').status_code,403);self.send()
        r=self.client.get('/credentials',base_url=self.base,headers=self.headers);self.assertEqual(r.status_code,200);self.assertIn(self.initial,r.text)
        self.assertEqual(self.temp.current(),self.initial)
        with patch('portal.time.time',return_value=time.time()+601):self.assertEqual(self.api('register/options').status_code,403)
    def make_credential(self,opts,register=False,origin=None,rp=None,uv=True,count=1):
        if register:self.private_key=ec.generate_private_key(ec.SECP256R1());self.credential_id=secrets.token_bytes(32)
        data=json.dumps({'type':'webauthn.create' if register else 'webauthn.get','challenge':opts['publicKey']['challenge'],'origin':origin or self.base,'crossOrigin':False}).encode()
        auth=hashlib.sha256((rp or self.settings['hostname']).encode()).digest()+bytes([1|(4 if uv else 0)|(64 if register else 0)])+count.to_bytes(4,'big')
        response={'clientDataJSON':b64(data)}
        if register:
            pub=self.private_key.public_key().public_numbers();cose=cbor2.dumps({1:2,3:-7,-1:1,-2:pub.x.to_bytes(32,'big'),-3:pub.y.to_bytes(32,'big')})
            auth+=b'\0'*16+len(self.credential_id).to_bytes(2,'big')+self.credential_id+cose
            response['attestationObject']=b64(cbor2.dumps({'fmt':'none','attStmt':{},'authData':auth}))
        else:response.update(authenticatorData=b64(auth),signature=b64(self.private_key.sign(auth+hashlib.sha256(data).digest(),ec.ECDSA(hashes.SHA256()))),userHandle=b64(self.keys.user_id))
        return dict(id=b64(self.credential_id),rawId=b64(self.credential_id),type='public-key',response=response)
    def enroll(self):
        self.assertEqual(self.send().status_code,200);o=self.api('register/options').json
        self.assertEqual(self.api('register/verify',challenge_id=o['challenge_id'],credential=self.make_credential(o,register=True,count=0),name='Test key').status_code,200)
    def test_signed_passkey_works_independently_without_rotation(self):
        self.enroll();self.client=self.app.test_client();o=self.api('auth/options').json
        self.assertEqual(o['publicKey']['userVerification'],'required')
        self.assertEqual(self.api('auth/verify',challenge_id=o['challenge_id'],credential=self.make_credential(o)).status_code,200)
        self.assertEqual(len(self.grants),2);self.assertEqual(self.temp.current(),self.initial)
    def test_replay_and_invalid_signature_rejected(self):
        self.enroll();o=self.api('auth/options').json;c=self.make_credential(o)
        self.assertEqual(self.api('auth/verify',challenge_id=o['challenge_id'],credential=c).status_code,200)
        self.assertEqual(self.api('auth/verify',challenge_id=o['challenge_id'],credential=c).status_code,401)
        o=self.api('auth/options').json;c=self.make_credential(o,count=2);c['response']['signature']=b64(b'invalid')
        self.assertEqual(self.api('auth/verify',challenge_id=o['challenge_id'],credential=c).status_code,401);self.assertEqual(len(self.grants),2)
    def test_origin_rp_and_user_verification_required(self):
        self.enroll()
        for kw in ({'origin':'https://evil.example'},{'rp':'evil.example'},{'uv':False}):
            o=self.api('auth/options').json;self.assertEqual(self.api('auth/verify',challenge_id=o['challenge_id'],credential=self.make_credential(o,**kw)).status_code,401)
        self.assertEqual(len(self.grants),1)
    def test_registration_user_verification_required(self):
        self.send();o=self.api('register/options').json
        self.assertEqual(self.api('register/verify',challenge_id=o['challenge_id'],credential=self.make_credential(o,register=True,uv=False)).status_code,400);self.assertEqual(self.keys.list(),[])
    def test_challenge_expiry_and_session_binding(self):
        self.enroll();o=self.api('auth/options').json;c=self.make_credential(o);self.client=self.app.test_client()
        self.assertEqual(self.api('auth/verify',challenge_id=o['challenge_id'],credential=c).status_code,401)
        o=self.api('auth/options').json;c=self.make_credential(o)
        with patch('auth_credentials.time.time',return_value=time.time()+121):self.assertEqual(self.api('auth/verify',challenge_id=o['challenge_id'],credential=c).status_code,401)
    def test_deleted_passkey_rejected(self):
        self.enroll();self.assertEqual(self.api('delete',key_id=b64(self.credential_id)).status_code,303)
        o=self.api('auth/options').json;self.assertEqual(self.api('auth/verify',challenge_id=o['challenge_id'],credential=self.make_credential(o)).status_code,401)

if __name__=='__main__':unittest.main()
