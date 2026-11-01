"""Drive a real Qt WebEngine window against an isolated Unix controller fixture."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--app-root', type=Path, default=ROOT)
args = parser.parse_args()
sys.path.insert(0, str(args.app_root))
import package_bootstrap
from desktop.control import Controller, make_server
from management import ManagementError, save_config, snapshot
from playwright.sync_api import sync_playwright


from desktop_ui_testing import Element, text_is, attribute_is, check_locked_settings, check_fixed_layout, check_idle_lock, port


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
    code = 'import sys;sys.path.insert(0,sys.argv[1]);sys.path.insert(1,sys.argv[3]);import package_bootstrap;from desktop.app import run;from tools.desktop_ui_testing import install_native_window_controls;install_native_window_controls();sys.exit(run(socket_path=sys.argv[2]))'
    log = (out / 'native.log').open('w+')
    command = [sys.executable, '-c', code, str(args.app_root), str(path), str(ROOT)]
    process = subprocess.Popen(command, env=env, stdout=log, stderr=log)
    try:
        with sync_playwright() as playwright:
            browser = None
            for attempt in range(100):
                if process.poll() is not None:
                    log.seek(0); raise AssertionError(f'Native process exited with {process.returncode}:\n' + log.read())
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
            assert not page.evaluate("Boolean(document.querySelector('.sidebar'))")
            assert not page.evaluate("Boolean(document.querySelector('[data-view=portal]'))")
            assert Element(page, '.welcome').evaluate('(el) => el.getBoundingClientRect().height > 0')
            assert not page.evaluate("Boolean(document.querySelector('.topbar [data-action=unlock], #lock-label, .setup-footer'))")
            check_fixed_layout(page, out / 'fixed-wizard.png')
            Element(page, '[data-action=begin]').click()
            text_is(page, 'h1', '外观设置')
            text_is(page, '.stepper small', '外观设置')
            assert page.evaluate("() => document.querySelector('.theme-options').closest('.card')===document.querySelector('.palette-options').closest('.card')")
            # Theme selection works before login and survives native status polls.
            for palette in ('blue', 'red'):
                Element(page, f'[data-action=setup-palette-{palette}]').click()
                for theme, label in (('system', '跟随系统'), ('light', '浅色'), ('dark', '深色')):
                    selector = f'[data-action=setup-theme-{theme}]'
                    Element(page, selector).click()
                    attribute_is(page, selector, 'aria-pressed', 'true')
                    attribute_is(page, selector, 'aria-label', label)
                    attribute_is(page, selector, 'title', label)
                    assert page.evaluate("() => document.querySelectorAll('.theme-options [aria-pressed=true]').length===1")
                    attribute_is(page, 'html', 'data-palette', 'hutao-red' if palette=='red' else 'fufu-blue')
                    if theme == 'system':
                        for system_theme in ('dark', 'light'):
                            page.evaluate('value => window.RdpTheme.system(value)', system_theme)
                            attribute_is(page, 'html', 'data-theme', system_theme)
                            attribute_is(page, selector, 'aria-pressed', 'true')
                    else:
                        attribute_is(page, 'html', 'data-theme', theme)
                    page.screenshot(path=str(out / f'{palette}-{theme}.png'), full_page=True)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.wait_for_timeout(2800)
            attribute_is(page, 'html', 'data-palette', 'hutao-red')
            attribute_is(page, 'html', 'data-theme', 'dark')
            assert controller.services.prefs['palette'] == 'fufu-blue'
            assert controller.services.prefs['theme'] == 'system'
            assert not page.evaluate("Boolean(document.querySelector('#dialog[open]'))")
            Element(page, '[data-form=setup-appearance] [type=submit]').click()
            text_is(page, 'h1', '管理账户')
            assert not page.evaluate("Boolean(document.querySelector('.topbar [data-action=unlock], #lock-label'))")
            assert page.evaluate("document.querySelector('[name=password]').value === ''")
            assert page.evaluate("!document.querySelector('[data-form=bootstrap]').noValidate")
            Element(page, '[name=password]').fill(password)
            Element(page, '[type=submit]').click()
            Element(page, '[data-form=mode]').wait()
            assert controller.services.prefs['palette'] == 'hutao-red'
            assert controller.services.prefs['theme'] == 'dark'
            assert json.loads(controller.services.path.read_text())['theme'] == 'dark'
            assert not page.evaluate("Boolean(document.querySelector('.topbar [data-action=unlock], #lock-label'))")
            Element(page, '[type=submit]').click()
            Element(page, '[data-form=platforms]').wait()
            Element(page, '[data-action=previous]').click()
            Element(page, '[name=mode][value=agent]').click()
            Element(page, '[data-form=mode] [type=submit]').click()
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
            Element(page, '[data-action=finish]').click()
            Element(page, '.sidebar').wait()
            assert not page.evaluate("Boolean(document.querySelector('[data-view=wizard], [data-view=portal]'))")
            text_is(page, '#main-content > .lead', '访问认证和隧道服务运行正常。')
            assert not page.evaluate("Boolean(document.querySelector('[data-action=open-portal]'))")
            check_idle_lock(page, process, out / 'native.log')
            Element(page, '[data-action=unlock]').click()
            Element(page, '#dialog [name=password]').fill(password)
            Element(page, '#dialog [type=submit]').click()
            text_is(page, '#lock-label', '设置已解锁')
            reading, release_read = threading.Event(), threading.Event()
            def delayed_secrets(*args, **kwargs):
                result = snapshot(*args, **kwargs)
                if kwargs.get('include_credentials'):
                    reading.set()
                    assert release_read.wait(10), 'automatic secret read was not released'
                return result
            with patch('desktop.control.snapshot', side_effect=delayed_secrets):
                try:
                    Element(page, '[data-view=settings]').click()
                    assert reading.wait(5), 'entering settings did not read saved secrets'
                    assert not page.evaluate("Boolean(document.querySelector('[data-action=show-secrets]'))")
                    Element(page, '[name=hostname]').fill('unsaved.example.test')
                    Element(page, '[data-eye=field-sakura_token]').click()
                    page.evaluate("""() => {
                        window.editedHostname = document.querySelector('[name=hostname]');
                        window.editedHostname.focus();window.editedHostname.setSelectionRange(3, 8);
                    }""")
                finally:
                    release_read.set()
                page.wait_for_function("() => document.querySelector('[name=sakura_token]')?.value==='fixture'")
            assert page.evaluate("() => window.editedHostname===document.querySelector('[name=hostname]') && window.editedHostname.value==='unsaved.example.test'"), 'automatic loading lost unsaved input'
            assert page.evaluate("() => document.activeElement===window.editedHostname && window.editedHostname.selectionStart===3 && window.editedHostname.selectionEnd===8"), 'automatic loading lost focus or selection'
            attribute_is(page, '[name=sakura_token]', 'type', 'text')
            check_fixed_layout(page, out / 'fixed-settings.png')
            page.wait_for_function("() => document.querySelector('[name=sakura_token]')?.value==='fixture'")
            Element(page, '[name=password]').fill('Unsaved-password-123!')
            Element(page, '[data-eye=field-password]').click()
            Element(page, '[data-action=unlock]').click()
            text_is(page, '#lock-label', '设置已锁定')
            assert page.evaluate("() => [...document.querySelectorAll('input[autocomplete=new-password]')].every(input=>input.value==='' && input.type==='password')"), 'locking retained window secrets'
            Element(page, '[data-action=unlock]').click()
            Element(page, '#dialog [name=password]').fill(password)
            Element(page, '#dialog [type=submit]').click()
            page.wait_for_function("() => document.querySelector('[name=sakura_token]')?.value==='fixture'")
            Element(page, '[data-view=credentials]').click()
            Element(page, '[name=allow_passkey_registration]').check()
            Element(page, '[data-form=passkey-policy] [type=submit]').click()
            text_is(page, '#toast', '立即生效', contains=True)
            assert json.loads(controller.target.config.read_text())['allow_passkey_registration'] is True
            Element(page, '[data-action=unlock]').click()
            text_is(page, '#lock-label', '设置已锁定')
            attribute_is(page, '[data-action=unlock]', 'aria-label', '解锁设置')
            check_locked_settings(page)
            Element(page, '[data-action=unlock]').click()
            Element(page, '#dialog-cancel').click()
            text_is(page, '#lock-label', '设置已锁定')
            attribute_is(page, '[data-action=theme-light]', 'disabled', '')
            Element(page, '[data-action=unlock]').click()
            Element(page, '#dialog [name=password]').fill(password)
            Element(page, '#dialog [type=submit]').click()
            text_is(page, '#lock-label', '设置已解锁')
            attribute_is(page, '[data-action=unlock] use', 'href', 'icons.svg#lock-open')
            assert page.evaluate("() => [...document.querySelectorAll('.appearance-options button, [data-form=preferences] input, [data-form=preferences] select, [data-form=preferences] [type=submit]')].every(e=>!e.disabled)")
            # A reply from an operation started before locking must not reopen
            # the unlock dialog or re-enable a protected button afterwards.
            preference_writer = controller.services.preferences
            started, release, finished = threading.Event(), threading.Event(), threading.Event()
            def late_unauthorized(changes):
                started.set()
                try:
                    assert release.wait(10), 'delayed preference request was not released'
                    raise ManagementError('会话已失效。', 401)
                finally:
                    finished.set()
            controller.services.preferences = late_unauthorized
            try:
                Element(page, '[data-action=theme-light]').click()
                assert started.wait(5)
                Element(page, '[data-action=unlock]').click()
                text_is(page, '#lock-label', '设置已锁定')
                release.set()
                assert finished.wait(5)
                page.wait_for_timeout(500)
                assert not page.evaluate("Boolean(document.querySelector('#dialog[open]'))")
                attribute_is(page, '[data-action=theme-light]', 'disabled', '')
            finally:
                release.set()
                controller.services.preferences = preference_writer
            Element(page, '[data-action=unlock]').click()
            Element(page, '#dialog [name=password]').fill(password)
            Element(page, '#dialog [type=submit]').click()
            text_is(page, '#lock-label', '设置已解锁')
            Element(page, '[data-view=maintenance]').click()
            Element(page, '[data-action=pause]').click()
            Element(page, '#dialog [type=submit]').click()
            text_is(page, '[data-action=pause]', '继续全部服务')
            assert controller.services.prefs['paused']
            # Hiding the tray preserves service state and the launcher can reopen.
            Element(page, '[data-view=appearance]').click()
            Element(page, '[name=show_tray]').uncheck()
            Element(page, '[data-action=theme-system]').click()
            attribute_is(page, '[data-action=theme-system]', 'aria-pressed', 'true')
            assert controller.services.prefs['theme'] == 'system'
            Element(page, '[data-action=theme-light]').click()
            attribute_is(page, '[data-action=theme-light]', 'aria-pressed', 'true')
            page.evaluate("() => window.RdpTheme.system('dark')")
            attribute_is(page, 'html', 'data-theme', 'light')
            assert controller.services.prefs['theme'] == 'light'
            assert json.loads(controller.services.path.read_text())['theme'] == 'light'
            assert not page.evaluate("document.querySelector('[name=show_tray]').checked"), 'theme changes lost an unsaved tray setting'
            Element(page, '[type=submit]').click()
            text_is(page, '#toast', '已保存', contains=True)
            assert not controller.services.prefs['show_tray']
            assert controller.services.prefs['theme'] == 'light', 'startup save reset the selected theme'
            second = subprocess.run(command, env=env, capture_output=True, timeout=15)
            assert second.returncode == 0, second.stderr.decode()
            Element(page, '.sidebar').wait()
            assert not page.evaluate("Boolean(document.querySelector('.setup-layout, [data-view=wizard], [data-view=portal]'))")
            Element(page, '[data-view=maintenance]').click()
            Element(page, '[data-action=exit]').click()
            text_is(page, '#dialog', '下次开机不自启动', contains=True)
            assert not page.evaluate("document.querySelector('#dialog [type=checkbox]').checked")
            assert page.evaluate("""() => {
                const box=document.querySelector('#dialog [type=checkbox]').getBoundingClientRect();
                const label=document.querySelector('#dialog .check span').getBoundingClientRect();
                return Math.abs(box.top+box.height/2-label.top-label.height/2)<1 && box.height===17;
            }"""), 'exit checkbox is not aligned with its label'
            page.screenshot(path=str(out / 'exit-dialog.png'))
            Element(page, '#dialog [type=checkbox]').check()
            Element(page, '#dialog [type=submit]').click()
            process.wait(timeout=15)
            assert controller.services.prefs['startup'] == 'manual'
            assert controller.services.prefs['paused']
            assert not errors, errors
            assert all(command[0] == 'systemctl' for command in commands)
            (out / 'result.json').write_text(json.dumps({'four_themes':True,'native_bridge':True,'wizard':True,
                'appearance_before_login':True,'locked_settings_read_only':True,'locked_interaction_hint':True,'idle_lock':True,'late_reply_keeps_settings_locked':True,'deferred_appearance_save':True,'combined_appearance_controls':True,'system_theme':True,'immediate_theme_save':True,'no_wizard_lock_button':True,'password_whitespace':True,'agent_registration':True,'clipboard':True,'persistent_pause':True,
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
