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
from playwright.sync_api import sync_playwright, expect


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
            expect(page.locator('.welcome')).to_be_visible()
            page.locator('[data-action=begin]').click()
            page.locator('[name=password]').fill(password)
            page.locator('[type=submit]').click()
            expect(page.locator('[data-form=mode]')).to_be_visible()
            page.locator('[type=submit]').click()
            expect(page.locator('[data-form=platforms]')).to_be_visible()
            # Four palettes render in the actual WebView, not in an HTML mock.
            for palette in ('fufu-blue', 'hutao-red'):
                for theme in ('light', 'dark'):
                    controller.services.preferences({'palette': palette, 'theme': theme})
                    page.evaluate('(v) => window.RdpTheme.set(v)', {'palette': palette, 'theme': theme})
                    page.wait_for_timeout(350)
                    expect(page.locator('html')).to_have_attribute('data-palette', palette)
                    expect(page.locator('html')).to_have_attribute('data-theme', theme)
                    page.screenshot(path=str(out / f'{palette}-{theme}.png'), full_page=True)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.locator('[data-view=agents]').click()
            expect(page.locator('[data-form=register-agent]')).to_be_visible()
            page.locator('[name=name]').fill('UI fixture Agent')
            page.locator('[type=submit]').click()
            page.locator('#dialog [name=password]').fill(password)
            page.locator('#dialog [type=submit]').click()
            expect(page.locator('[data-action=copy-mcp]')).to_be_visible()
            assert len(controller.authority.data['agents']) == 1
            page.locator('[data-action=copy-mcp]').click()
            expect(page.locator('#toast')).to_contain_text('已复制')
            # Install only fixture configuration. No host service is touched.
            words = root / 'objects.json'; words.write_text(json.dumps(['测试' + chr(0x4e00+i) for i in range(2048)]))
            save_config(controller.target, {'hostname':'auth.example.test','rdp_address':'rdp.example.test:3389',
                'tunnel_id': 123456, 'password':'Remote-password-1234!', 'sakura_token':'fixture', 'wordlist_path':str(words)}, create=True)
            page.locator('[data-view=maintenance]').click()
            page.locator('[data-action=pause]').click()
            page.locator('#dialog [type=submit]').click()
            expect(page.locator('[data-action=pause]')).to_have_text('继续全部服务')
            assert controller.services.prefs['paused']
            # Hiding the tray preserves service state and the launcher can reopen.
            page.locator('[data-view=appearance]').click()
            page.locator('[name=show_tray]').uncheck()
            page.locator('[type=submit]').click()
            expect(page.locator('#toast')).to_contain_text('已保存')
            assert not controller.services.prefs['show_tray']
            second = subprocess.run([sys.executable, '-c', code, str(args.app_root), str(path)], env=env, capture_output=True, timeout=15)
            assert second.returncode == 0, second.stderr.decode()
            page.locator('[data-view=maintenance]').click()
            page.locator('[data-action=exit]').click()
            expect(page.locator('#dialog')).to_contain_text('下次开机不自启动')
            expect(page.locator('#dialog [type=checkbox]')).not_to_be_checked()
            page.locator('#dialog [type=checkbox]').check()
            page.locator('#dialog [type=submit]').click()
            process.wait(timeout=15)
            assert controller.services.prefs['startup'] == 'manual'
            assert controller.services.prefs['paused']
            assert not errors, errors
            assert all(command[0] == 'systemctl' for command in commands)
            (out / 'result.json').write_text(json.dumps({'four_themes':True,'native_bridge':True,'wizard':True,
                'password_whitespace':True,'agent_registration':True,'clipboard':True,'persistent_pause':True,
                'hide_tray':True,'single_instance':True,'exit_checkbox':True,'page_errors':errors}, indent=2))
    finally:
        if process.poll() is None:
            process.terminate(); process.wait(timeout=10)
        server.shutdown(); server.server_close(); thread.join(); log.close()
print('Native WebView, four themes, wizard, Agent registration, persistent pause, hidden tray, launcher and exit checks passed.')
