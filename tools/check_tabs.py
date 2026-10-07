"""Offline browser regression for local method switching; uses mocked authentication endpoints."""
from pathlib import Path
from urllib.parse import urlparse,parse_qs
import json
import shutil
from jinja2 import Template
from playwright.sync_api import sync_playwright, expect
source=Template((Path(__file__).resolve().parents[1] / 'portal.html').read_text())
def html(method='password',turnstile=True):
 return source.render(nonce='preview',method=method,manage=False,success=False,csrf='sample',ipv6=False,client_ip='203.0.113.42',turnstile_site_key='test-key' if turnstile else '',message='')
with sync_playwright() as p:
 chrome=shutil.which('google-chrome')
 browser=p.chromium.launch(**({'executable_path':chrome} if chrome else {}))
 page=browser.new_page(reduced_motion='reduce');errors=[];posts=[];documents=[]
 page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  req=r.request;url=req.url
  if 'challenges.cloudflare.com' in url:
   r.fulfill(content_type='text/javascript',body="window.actions=[];window.turnstile={render:(el,opts)=>{actions.push(opts.action);return actions.length},remove:()=>{},reset:()=>{},getResponse:()=> 'test-token'};onTurnstileReady();")
  elif '/passkeys/auth/options' in url:
   posts.append((url,req.post_data_json));r.fulfill(json={'challenge_id':'sample','publicKey':{'challenge':'AA','allowCredentials':[]}})
  elif '/passkeys/auth/verify' in url:
   posts.append((url,req.post_data_json));r.fulfill(status=400,json={'error':'模拟验证完成'})
  elif req.method=='POST':
   posts.append((url,req.post_data));r.fulfill(body='submitted')
  else:
   if req.resource_type=='document':documents.append(url)
   method=parse_qs(urlparse(url).query).get('method',['password'])[0]
   r.fulfill(content_type='text/html',body=html(method),headers={'Content-Security-Policy':"default-src 'none'; img-src data:; style-src 'nonce-preview'; script-src 'nonce-preview' https://challenges.cloudflare.com; connect-src 'self'; form-action 'self'"})
 page.route('**/*',route)
 page.goto('http://127.0.0.1:18125/?method=password');page.wait_for_function('window.actions?.length === 1')
 page.locator('#password').fill('Demo-only-123!');expect(page.locator('#current-ip')).to_have_text('203.0.113.42')
 page.evaluate('window.sentinel=123')
 for theme in ['dark','light']:
  page.emulate_media(color_scheme=theme)
  for width in [320,390,768,1024,1440]:
   page.set_viewport_size(dict(width=width,height=1000))
   for label,method in [('临时密码','temporary'),('通行密钥','passkey'),('固定密码','password')]:
    page.get_by_role('link',name=label,exact=True).click()
    assert page.locator('[name=method]').input_value()==method
    assert page.evaluate('window.sentinel')==123
    assert page.evaluate('actions.at(-1)')=='login_'+method
    assert page.locator('#current-ip').inner_text()=='203.0.113.42';assert page.locator('[name=ipv4]').count()==0
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert page.locator('.method-panel:not([hidden])').count()==1
 assert len(documents)==1,documents
 assert page.locator('#password').input_value()=='Demo-only-123!'
 page.go_back();assert page.locator('[name=method]').input_value()=='passkey'
 page.go_forward();assert page.locator('[name=method]').input_value()=='password'
 page.get_by_role('link',name='临时密码',exact=True).click()
 for i,word in enumerate(['钻石','苹果','蛋糕'],1):page.locator(f'#temporary-{i}').fill(word)
 page.get_by_role('button',name='显示密码',exact=True).click();assert page.locator('#temporary-1').get_attribute('type')=='text'
 page.get_by_role('link',name='固定密码',exact=True).click();page.get_by_role('link',name='临时密码',exact=True).click();assert page.locator('#temporary-1').get_attribute('type')=='password'
 data=page.locator('#auth-form').evaluate('(f)=>Object.fromEntries(new FormData(f))');assert data['temporary_1']=='钻石' and 'password' not in data and data['csrf']=='sample'
 page.get_by_role('link',name='通行密钥',exact=True).click()
 page.evaluate("""navigator.credentials.get = async () => ({id:'sample',rawId:new Uint8Array([0]).buffer,type:'public-key',response:{clientDataJSON:new Uint8Array([0]).buffer,authenticatorData:new Uint8Array([0]).buffer,signature:new Uint8Array([0]).buffer},getClientExtensionResults:()=>({})})""")
 page.get_by_role('button',name='使用通行密钥连接',exact=True).click()
 expect(page.locator('#passkey-status')).to_have_text('模拟验证完成')
 assert posts[-1][0].endswith('/verify') and posts[-1][1]['csrf']=='sample'
 page.evaluate("() => {navigator.credentials.get = ({signal}) => new Promise((resolve,reject)=>{signal.addEventListener('abort',()=>{window.cancelled=true;reject(new DOMException('cancel','AbortError'))})});}")
 page.get_by_role('button',name='使用通行密钥连接',exact=True).click();page.wait_for_timeout(200)
 page.get_by_role('link',name='固定密码',exact=True).click();page.wait_for_function('window.cancelled === true')
 page.wait_for_function("!document.querySelector('.authorize-button').disabled")
 page.get_by_role('button',name='认证并授权',exact=True).click();page.wait_for_url('**/authorize')
 data=parse_qs(posts[-1][1]);assert data['method']==['password'] and not any(k.startswith('temporary_') for k in data)
 assert not errors,errors
 nojs=browser.new_page(java_script_enabled=False);nojs.route('**/*',lambda r:r.fulfill(content_type='text/html',body=html('temporary',False)))
 nojs.goto('http://127.0.0.1:18125/');assert nojs.locator('.method-panel:not([hidden])').get_attribute('data-method-panel')=='temporary'
 browser.close()
print('No document reload, history, preserved input, disabled inactive fields, Turnstile actions, passkey after switching/cancellation, native password submit, themes and sizes passed')
