"""Drive a real Qt WebEngine window against an isolated Unix controller fixture."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--app-root', type=Path, default=ROOT)
args = parser.parse_args()
sys.path.insert(0, str(args.app_root))
import package_bootstrap
from desktop.control import Controller, make_server
from management import save_config
from playwright.sync_api import sync_playwright


# Use browser-native DOM checks and physical input. Current Playwright's locator
# assertions require Element.checkVisibility, absent in Ubuntu's Qt 6.4 engine.
class Element:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    def wait(self):
        self.page.wait_for_function("s => {const e=document.querySelector(s);return e && e.getBoundingClientRect().height>0}", arg=self.selector)
        return self

    def click(self):
        self.wait()
        self.page.wait_for_function("s => !document.querySelector(s).disabled", arg=self.selector)
        point = self.page.evaluate("s => {const e=document.querySelector(s);e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]}", self.selector)
        self.page.mouse.click(*point)

    def fill(self, value):
        self.wait()
        self.page.evaluate("s => {const e=document.querySelector(s);e.focus();e.select()}", self.selector)
        self.page.keyboard.insert_text(value)

    def check(self):
        if not self.page.evaluate('s => document.querySelector(s).checked', self.selector): self.click()

    def uncheck(self):
        if self.page.evaluate('s => document.querySelector(s).checked', self.selector): self.click()

    def evaluate(self, expression):
        self.wait()
        return self.page.evaluate('v => ('+expression+')(document.querySelector(v))', self.selector)


def text_is(page, selector, value, contains=False):
    page.wait_for_function("v => {const e=document.querySelector(v[0]);return e && (v[2] ? e.textContent.includes(v[1]) : e.textContent===v[1])}", arg=[selector,value,contains])


def attribute_is(page, selector, name, value):
    page.wait_for_function("v => document.querySelector(v[0])?.getAttribute(v[1])===v[2]", arg=[selector,name,value])


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


out = ROOT / '.build/desktop-check'
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='rdp-desktop-test-') as directory:
    root = Path(directory)
    commands = []
    def runner(command):
        commands.append(command)
        return 'LoadState=loaded\nActiveState=active\nSubState=running\n'
    controller = Controller(root, runner)
    password = ' Local-management-123! '
    controller.authority.initialize(os.getuid(), password)
    controller.services.save()
    path = root / 'control.sock'
    server = make_server(controller, path)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    debug = port()
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QTWEBENGINE_REMOTE_DEBUGGING=f'127.0.0.1:{debug}',
               QTWEBENGINE_CHROMIUM_FLAGS='--disable-gpu --no-sandbox', XDG_CONFIG_HOME=str(root / 'config'),
               XDG_CACHE_HOME=str(root / 'cache'))
    code = 'import sys;sys.path.insert(0,sys.argv[1]);import package_bootstrap;from desktop.app import run;sys.exit(run(socket_path=sys.argv[2]))'
    log = (out / 'native.log').open('w+')
    process = subprocess.Popen([sys.executable, '-c', code, str(args.app_root), str(path)], env=env, stdout=log, stderr=log)
    try:
        with sync_playwright() as playwright:
            browser = None
            for attempt in range(100):
                if process.poll() is not None:
                    log.seek(0); raise AssertionError(log.read())
                try:
                    browser = playwright.chromium.connect_over_cdp(f'http://127.0.0.1:{debug}', timeout=3000, no_defaults=True)
                    break
                except Exception as exc:
                    if attempt == 99: print(str(exc), file=sys.stderr)
                    time.sleep(.2)
            assert browser, 'native debugging endpoint did not open'
            page = browser.contexts[0].pages[0]
            page.emulate_media(reduced_motion='reduce')
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            Element(page, '.welcome').wait()
            assert Element(page, '.welcome').evaluate('(el) => el.getBoundingClientRect().height > 0')
            Element(page, '[data-action=begin]').click()
            Element(page, '[name=password]').fill(password)
            Element(page, '[type=submit]').click()
            Element(page, '[data-form=mode]').wait()
            Element(page, '[type=submit]').click()
            Element(page, '[data-form=platforms]').wait()
            # Four palettes render in the actual WebView, not in an HTML mock.
            for palette in ('fufu-blue', 'hutao-red'):
                for theme in ('light', 'dark'):
                    controller.services.preferences({'palette': palette, 'theme': theme})
                    page.evaluate('(v) => window.RdpTheme.set(v)', {'palette': palette, 'theme': theme})
                    page.wait_for_timeout(350)
                    attribute_is(page, 'html', 'data-palette', palette)
                    attribute_is(page, 'html', 'data-theme', theme)
                    page.screenshot(path=str(out / f'{palette}-{theme}.png'), full_page=True)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            Element(page, '[data-view=agents]').click()
            Element(page, '[data-form=register-agent]').wait()
            Element(page, '[name=name]').fill('UI fixture Agent')
            Element(page, '[type=submit]').click()
            Element(page, '#dialog [name=password]').fill(password)
            Element(page, '#dialog [type=submit]').click()
            Element(page, '[data-action=copy-mcp]').wait()
            assert len(controller.authority.data['agents']) == 1
            Element(page, '[data-action=copy-mcp]').click()
            text_is(page, '#toast', '已复制', contains=True)
            # Install only fixture configuration. No host service is touched.
            words = root / 'objects.json'; words.write_text(json.dumps(['测试' + chr(0x4e00+i) for i in range(2048)]))
            save_config(controller.target, {'hostname':'auth.example.test','rdp_address':'rdp.example.test:3389',
                'tunnel_id': 123456, 'password':'Remote-password-1234!', 'sakura_token':'fixture', 'wordlist_path':str(words)}, create=True)
            Element(page, '[data-view=maintenance]').click()
            Element(page, '[data-action=pause]').click()
            Element(page, '#dialog [type=submit]').click()
            text_is(page, '[data-action=pause]', '继续全部服务')
            assert controller.services.prefs['paused']
            # Hiding the tray preserves service state and the launcher can reopen.
            Element(page, '[data-view=appearance]').click()
            Element(page, '[name=show_tray]').uncheck()
            Element(page, '[type=submit]').click()
            text_is(page, '#toast', '已保存', contains=True)
            assert not controller.services.prefs['show_tray']
            second = subprocess.run([sys.executable, '-c', code, str(args.app_root), str(path)], env=env, capture_output=True, timeout=15)
            assert second.returncode == 0, second.stderr.decode()
            Element(page, '[data-view=maintenance]').click()
            Element(page, '[data-action=exit]').click()
            text_is(page, '#dialog', '下次开机不自启动', contains=True)
            assert not page.evaluate("document.querySelector('#dialog [type=checkbox]').checked")
            Element(page, '#dialog [type=checkbox]').check()
            Element(page, '#dialog [type=submit]').click()
            process.wait(timeout=15)
            assert controller.services.prefs['startup'] == 'manual'
            assert controller.services.prefs['paused']
            assert not errors, errors
            assert all(command[0] == 'systemctl' for command in commands)
            (out / 'result.json').write_text(json.dumps({'four_themes':True,'native_bridge':True,'wizard':True,
                'password_whitespace':True,'agent_registration':True,'clipboard':True,'persistent_pause':True,
                'hide_tray':True,'single_instance':True,'exit_checkbox':True,'page_errors':errors}, indent=2))
    except Exception:
        if 'page' in locals():
            try:
                page.screenshot(path=str(out / 'failure.png'), full_page=True)
                (out / 'failure.html').write_text(page.content())
            except Exception:
                pass
        raise
    finally:
        if process.poll() is None:
            process.terminate(); process.wait(timeout=10)
        server.shutdown(); server.server_close(); thread.join(); log.close()
print('Native WebView, four themes, wizard, Agent registration, persistent pause, hidden tray, launcher and exit checks passed.')
