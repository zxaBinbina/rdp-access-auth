"""Exercise the real demo WebView while forbidding controller and system calls."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from playwright.sync_api import sync_playwright
from desktop_ui_testing import Element, text_is, attribute_is, check_locked_settings, check_fixed_layout, check_idle_lock, native_window_event, port

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--app-root', type=Path, default=ROOT)
args = parser.parse_args()
out = ROOT / '.build/demo-check'
out.mkdir(parents=True, exist_ok=True)
code = '''import sys
import faulthandler
faulthandler.enable()
sys.path.insert(0, sys.argv[1])
sys.path.insert(1, sys.argv[3])
import package_bootstrap
from unittest.mock import patch
from desktop.app import run
from tools.desktop_ui_testing import install_native_window_controls
install_native_window_controls()
with patch('desktop.app.Client', side_effect=AssertionError('demo contacted live controller')), \\
     patch('desktop.app.subprocess.run', side_effect=AssertionError('demo ran a system command')), \\
     patch('desktop.app.atomic_private_write', side_effect=AssertionError('demo wrote credentials')):
    sys.exit(run('overview', demo=True, socket_path=sys.argv[2]))
'''

with tempfile.TemporaryDirectory(prefix='rdp-demo-test-') as directory:
    root = Path(directory)
    debug = port()
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QTWEBENGINE_REMOTE_DEBUGGING=f'127.0.0.1:{debug}',
               QTWEBENGINE_CHROMIUM_FLAGS='--disable-gpu --no-sandbox', XDG_CONFIG_HOME=str(root / 'config'),
               XDG_CACHE_HOME=str(root / 'cache'))
    log = (out / 'native.log').open('w+')
    command = [sys.executable, '-c', code, str(args.app_root), str(root / 'unused.sock'), str(ROOT)]
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
                except Exception:
                    time.sleep(.2)
            assert browser, 'demo window did not open'
            page = browser.contexts[0].pages[0]
            errors, external = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: external.append(request.url) if request.url.startswith(('http:', 'https:')) else None)
            page.emulate_media(reduced_motion='reduce')
            Element(page, '.sidebar').wait()
            assert not page.evaluate("Boolean(document.querySelector('[data-action=open-portal]'))")
            text_is(page, '#lock-label', '设置已锁定')
            check_locked_settings(page)
            Element(page, '[data-action=theme-dark]').click(allow_disabled=True)
            page.wait_for_function("() => !document.getElementById('locked-hint').hidden")
            page.screenshot(path=str(out / 'locked-setting-bubble.png'))
            assert not page.evaluate("document.getElementById('locked-hint').hidden")
            Element(page, '[data-action=unlock]').click()
            assert page.evaluate("document.getElementById('locked-hint').hidden")
            sample_password = page.evaluate("document.querySelector('#dialog [name=password]').value")
            Element(page, '#dialog [name=password]').fill('incorrect-password')
            Element(page, '#dialog [type=submit]').click()
            text_is(page, '#dialog .inline-error', '管理密码不正确。')
            attribute_is(page, '[data-action=theme-dark]', 'disabled', '')
            Element(page, '#dialog [name=password]').fill(sample_password)
            Element(page, '#dialog [type=submit]').click()
            text_is(page, '#lock-label', '设置已解锁')
            assert not page.evaluate("document.querySelector('[data-action=theme-dark]').disabled")
            Element(page, '[data-action=unlock]').click()
            text_is(page, '#lock-label', '设置已锁定')
            assert not page.evaluate("Boolean(document.querySelector('[data-view=wizard]'))")
            Element(page, '[data-view=portal]').wait()
            Element(page, '[data-action=demo-wizard]').click()
            Element(page, '.welcome').wait()
            assert not page.evaluate("Boolean(document.querySelector('.sidebar'))")
            assert not page.evaluate("Boolean(document.querySelector('[data-view=portal]'))")
            text_is(page, '.demo-banner', '配置向导演示', contains=True)
            text_is(page, '.version', '开发版', contains=True)
            assert not page.evaluate("Boolean(document.querySelector('.topbar [data-action=unlock], #lock-label, .setup-footer'))")
            check_fixed_layout(page, out / 'fixed-wizard.png')
            Element(page, '[data-action=begin]').click()
            text_is(page, 'h1', '外观设置')
            text_is(page, '.stepper small', '外观设置')
            assert not page.evaluate("Boolean(document.querySelector('.setup-layout .demo-steps'))")
            Element(page, '[data-action=setup-palette-red]').click()
            Element(page, '[data-action=setup-theme-light]').click()
            attribute_is(page, '[data-action=setup-theme-light]', 'aria-pressed', 'true')
            page.keyboard.press('Tab')
            page.keyboard.press('Enter')
            attribute_is(page, '[data-action=setup-theme-dark]', 'aria-pressed', 'true')
            attribute_is(page, 'html', 'data-palette', 'hutao-red')
            attribute_is(page, 'html', 'data-theme', 'dark')
            page.wait_for_timeout(2800)
            attribute_is(page, 'html', 'data-theme', 'dark')
            assert not page.evaluate("Boolean(document.querySelector('#dialog[open]'))")
            Element(page, '[data-form=setup-appearance] [type=submit]').click()
            text_is(page, 'h1', '管理账户')
            text_is(page, '.stepper small', '管理账户')
            assert not page.evaluate("Boolean(document.querySelector('.topbar [data-action=unlock], #lock-label'))")
            password = page.evaluate("document.querySelector('[name=password]').value")
            assert len(password) >= 16
            assert page.evaluate("document.querySelector('[name=repeat_password]').value") == password

            def submit_blank(name):
                Element(page, f'[data-form={name}]').wait()
                # Clear every editable field; the ordinary production form still requires them.
                page.evaluate("name => document.querySelectorAll('[data-form='+name+'] input:not([type=radio]):not([type=checkbox])').forEach(e=>{e.value='';e.dispatchEvent(new Event('input',{bubbles:true}))})", name)
                Element(page, f'[data-form={name}] [type=submit]').click()

            submit_blank('bootstrap')
            Element(page, '[data-form=mode] [type=submit]').click()
            for name in ('platforms', 'network', 'access'):
                submit_blank(name)
            Element(page, '[data-action=apply]').click()
            Element(page, '#dialog [type=submit]').click()
            Element(page, '[data-action=finish]').click()
            text_is(page, '#main-content > .lead', '访问认证和隧道服务运行正常。')
            check_idle_lock(page, process, out / 'native.log')
            Element(page, '[data-action=unlock]').click()
            Element(page, '#dialog [type=submit]').click()
            text_is(page, '#lock-label', '设置已解锁')

            # Idle status polls must preserve the page, focus and default motion.
            page.emulate_media(reduced_motion='no-preference')
            page.wait_for_timeout(500)
            page.evaluate("""() => {
                document.querySelector('[data-action=pause]').focus();
                window.idlePreview = {content:document.getElementById('main-content'),
                    focus:document.activeElement, animations:0};
                document.addEventListener('animationstart', event => {
                    if (event.target.id === 'main-content') window.idlePreview.animations++;
                });
            }""")
            page.wait_for_timeout(5600)  # Observe more than two native 2.5-second polls.
            assert page.evaluate("() => window.idlePreview.content === document.getElementById('main-content')"), 'idle status polls rebuilt the page'
            assert page.evaluate("() => window.idlePreview.focus === document.activeElement"), 'idle status polls lost keyboard focus'
            assert page.evaluate("() => window.idlePreview.animations === 0"), 'idle status polls replayed the entrance animation'
            page.evaluate("() => {window.idlePreview.services=document.querySelector('.service-grid');window.idlePreview.heading=document.querySelector('h1');}")
            Element(page, '[data-action=pause]').click()
            Element(page, '#dialog [type=submit]').click()
            text_is(page, '#main-content > .lead', '连接服务已暂停，请手动恢复。')
            assert page.evaluate("() => window.idlePreview.content===document.getElementById('main-content') && window.idlePreview.services===document.querySelector('.service-grid') && window.idlePreview.heading===document.querySelector('h1')"), 'status changes replaced unaffected DOM'
            assert page.evaluate("() => window.idlePreview.animations === 0"), 'status changes replayed entrance motion'
            Element(page, '[data-action=pause]').click()
            Element(page, '#dialog [type=submit]').click()
            text_is(page, '#main-content > .lead', '访问认证和隧道服务运行正常。')
            Element(page, '[data-action=unlock]').click()
            text_is(page, '#lock-label', '设置已锁定')
            attribute_is(page, '[data-action=unlock]', 'aria-label', '解锁设置')
            attribute_is(page, '[data-action=unlock] use', 'href', 'icons.svg#shield-check')
            Element(page, '[data-action=unlock]').click()
            Element(page, '#dialog [type=submit]').click()
            text_is(page, '#lock-label', '设置已解锁')
            attribute_is(page, '[data-action=unlock]', 'aria-label', '锁定设置')
            attribute_is(page, '[data-action=unlock] use', 'href', 'icons.svg#lock-open')
            assert page.evaluate("() => window.idlePreview.content===document.getElementById('main-content')"), 'locking replaced the page'
            page.emulate_media(reduced_motion='reduce')

            # The other wizard branches are available without configuration too.
            Element(page, '[data-action=demo-wizard]').click()
            Element(page, '[data-action=begin]').click()
            Element(page, '[data-form=setup-appearance] [type=submit]').click()
            submit_blank('bootstrap')
            text_is(page, '.stepper small', '部署方式')
            Element(page, '[name=mode][value=manual]').click()
            Element(page, '[data-form=mode] [type=submit]').click()
            Element(page, '[name=cloudflare_tunnel_token]').wait()
            submit_blank('platforms')
            Element(page, '[name=sakura_tunnel_id]').wait()
            submit_blank('network')
            submit_blank('access')
            Element(page, '[data-action=back-access]').click()
            for step in ('网络设置', '平台凭据', '部署方式'):
                Element(page, '[data-action=previous]').click()
                text_is(page, '.stepper small', step)
            Element(page, '[name=mode][value=agent]').click()
            Element(page, '[data-form=mode] [type=submit]').click()
            Element(page, '[data-form=register-agent]').wait()
            Element(page, '[data-danger=true][data-action=approve-agent]').click()
            Element(page, '#dialog [type=submit]').click()
            page.wait_for_function("() => !document.querySelector('[data-danger=true][data-action=approve-agent]')")
            Element(page, '[data-form=register-agent] [name=name]').fill('尚未保存的 Agent 名称')
            page.evaluate("""() => {
                window.editedAgent = document.querySelector('[data-form=register-agent] [name=name]');
                window.editedAgent.focus();window.editedAgent.setSelectionRange(2, 5);
                document.querySelector('[data-action=deny-agent]').click();
            }""")
            page.wait_for_function("() => !document.querySelector('[data-action=deny-agent]')")
            assert page.evaluate("() => window.editedAgent===document.querySelector('[data-form=register-agent] [name=name]') && window.editedAgent.value==='尚未保存的 Agent 名称'"), 'list removal replaced an unrelated form'
            assert page.evaluate("() => document.activeElement===window.editedAgent && window.editedAgent.selectionStart===2 && window.editedAgent.selectionEnd===5"), 'list removal lost form focus or selection'
            submit_blank('register-agent')
            Element(page, '#dialog [type=submit]').click()
            Element(page, '[data-action=copy-mcp]').wait()
            text_is(page, '#main-content', '未创建身份文件', contains=True)

            Element(page, '[data-action=demo-main]').click()
            for view in ('overview', 'settings', 'credentials', 'agents', 'maintenance', 'appearance'):
                Element(page, f'[data-view={view}]').click()
                page.wait_for_timeout(150)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), view
                page.screenshot(path=str(out / f'{view}.png'), full_page=True)
            Element(page, '[data-view=settings]').click()
            page.wait_for_function("() => document.querySelector('[name=sakura_token]').value.startsWith('demo-')")
            assert not page.evaluate("Boolean(document.querySelector('[data-action=show-secrets]'))")
            check_fixed_layout(page, out / 'fixed-settings.png')
            Element(page, '[name=hostname]').fill('unsaved.example.test')
            Element(page, '[data-eye=field-sakura_token]').click()
            page.evaluate("""() => {
                window.editedHostname = document.querySelector('[name=hostname]');
                window.editedHostname.focus();window.editedHostname.setSelectionRange(3, 8);
            }""")
            page.wait_for_timeout(3000)
            assert page.evaluate("() => window.editedHostname===document.querySelector('[name=hostname]') && window.editedHostname.value==='unsaved.example.test'"), 'incremental update lost unsaved input'
            assert page.evaluate("() => document.activeElement===window.editedHostname && window.editedHostname.selectionStart===3 && window.editedHostname.selectionEnd===8"), 'incremental update lost input focus or selection'
            attribute_is(page, '[name=sakura_token]', 'type', 'text')
            Element(page, '[data-view=appearance]').click()
            # Icon controls save immediately, including follow-system mode.
            for palette in ('blue', 'red'):
                Element(page, f'[data-action=palette-{palette}]').click()
                for theme in ('system', 'light', 'dark'):
                    selector = f'[data-action=theme-{theme}]'
                    Element(page, selector).click()
                    attribute_is(page, selector, 'aria-pressed', 'true')
                    assert page.evaluate("() => document.querySelectorAll('.theme-options [aria-pressed=true]').length===1")
                    if theme == 'system':
                        for system_theme in ('dark', 'light'):
                            page.evaluate('value => window.RdpTheme.system(value)', system_theme)
                            attribute_is(page, 'html', 'data-theme', system_theme)
                            attribute_is(page, selector, 'aria-pressed', 'true')
                    else:
                        attribute_is(page, 'html', 'data-theme', theme)
                    page.screenshot(path=str(out / f'{palette}-{theme}.png'), full_page=True)
            Element(page, '[data-form=preferences] [type=submit]').click()
            text_is(page, '#toast', '已保存', contains=True)
            attribute_is(page, '[data-action=theme-dark]', 'aria-pressed', 'true')

            Element(page, '[data-view=credentials]').click()
            Element(page, '[name=allow_passkey_registration]').check()
            Element(page, '[data-form=passkey-policy] [type=submit]').click()
            text_is(page, '#toast', '演示配置已更新。')
            Element(page, '[data-view=overview]').click()
            Element(page, '[data-view=credentials]').click()
            assert page.evaluate("document.querySelector('[name=allow_passkey_registration]').checked")
            Element(page, '[data-view=portal]').click()
            def portal_frame(screen):
                page.wait_for_function("s=>{const f=document.querySelector('iframe');return f?.contentDocument?.getElementById('demo-portal-data') && new URL(f.contentWindow.location.href).searchParams.get('screen')===s}", arg=screen)
                return page.frames[-1]

            for screen in ('password', 'temporary', 'passkey', 'success', 'credentials', 'credentials-passkey', 'error', 'ipv6'):
                Element(page, f'[data-screen={screen}]').click()
                frame = portal_frame(screen)
                frame.wait_for_selector('main')
                frame.wait_for_function("() => Boolean(document.getElementById('demo-portal-data'))")
                if screen == 'credentials':
                    assert frame.evaluate("Boolean(document.getElementById('register-key'))")
                    assert not frame.evaluate("Boolean(document.querySelector('form[action=\"/passkeys/delete\"]'))")
                if screen == 'credentials-passkey':
                    assert frame.evaluate("document.querySelectorAll('form[action=\"/passkeys/delete\"]').length===1")
                    assert not frame.evaluate("Boolean(document.getElementById('register-key'))")
                page.screenshot(path=str(out / f'portal-{screen}.png'), full_page=True)
            Element(page, '[data-screen=password]').click()
            frame = portal_frame('password')
            assert frame.evaluate("document.getElementById('password').value")
            frame.evaluate("document.getElementById('password').value='';document.querySelector('#auth-form [type=submit]').click()")
            frame.wait_for_selector('.result-mark')
            page.evaluate("() => {window.previewFrame=document.querySelector('iframe');window.previewDocument=window.previewFrame.contentDocument;}")
            Element(page, '[data-action=unlock]').click()
            text_is(page, '#lock-label', '设置已锁定')
            assert page.evaluate("() => window.previewFrame===document.querySelector('iframe') && window.previewDocument===window.previewFrame.contentDocument"), 'locking reloaded the unchanged portal preview'
            Element(page, '[data-action=unlock]').click()
            Element(page, '#dialog [type=submit]').click()
            text_is(page, '#lock-label', '设置已解锁')
            assert page.evaluate("() => window.previewDocument===document.querySelector('iframe').contentDocument"), 'unlocking reloaded the unchanged portal preview'

            Element(page, '[data-view=maintenance]').click()
            Element(page, '[data-action=pause]').click()
            Element(page, '#dialog [type=submit]').click()
            text_is(page, '[data-action=pause]', '继续全部服务')
            closed = native_window_event(process, 'close', out / 'native.log')
            assert not closed['visible'] and closed['tray_visible'], closed
            text_is(page, '#lock-label', '设置已锁定')
            assert process.poll() is None, 'closing the window exited the demo'
            # The launcher reopens the same process, with its simulated state intact.
            second = subprocess.run(command, env=env, capture_output=True, timeout=15)
            assert second.returncode == 0, second.stderr.decode()
            assert native_window_event(process, 'inspect', out / 'native.log')['visible']
            text_is(page, '#main-content > .lead', '连接服务已暂停，请手动恢复。')
            Element(page, '[data-action=demo-reset]').click()
            Element(page, '[data-action=demo-wizard]').click()
            Element(page, '[data-action=begin]').click()
            Element(page, '[data-form=setup-appearance] [type=submit]').click()
            assert page.evaluate("document.querySelector('[name=password]').value") != password
            Element(page, '[data-action=demo-main]').click()
            text_is(page, '#main-content > .lead', '访问认证和隧道服务运行正常。')
            Element(page, '[data-action=demo-close]').click()
            process.wait(timeout=15)
            assert process.returncode == 0
            assert not errors, errors
            assert not external, external
            assert not list(root.rglob('*.conf')), 'demo persisted QSettings'
            assert not list(root.rglob('*.json')), 'demo persisted configuration or identity data'
            (out / 'result.json').write_text(json.dumps({'blank_wizard':True, 'random_samples':True,
                'all_management_pages':True,'portal_screens':8,'four_themes':True,'reset':True,'stable_idle_preview':True,
                'appearance_before_account':True,'locked_settings_read_only':True,'locked_interaction_hint':True,'idle_lock':True,'close_to_tray':True,'demo_password_validation':True,'theme_keyboard_controls':True,'system_theme':True,'no_wizard_lock_button':True,'incremental_status_updates':True,'lock_button_updates':True,'preserve_unsaved_input':True,
                'no_live_controller':True,'no_system_commands':True,'no_configuration_files':True,
                'external_requests':external,'page_errors':errors}, indent=2))
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
        log.close()
print('Demo WebView: empty forms, random samples, all pages, portal states, themes and isolation passed.')
