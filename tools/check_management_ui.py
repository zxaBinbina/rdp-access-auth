"""Real-browser regression using temporary files and simulated systemd operations only."""
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from auth_guard import AuthGuard
from management import Target, read_config, save_config
from management_web import create_server


def main():
    with tempfile.TemporaryDirectory(prefix='rdp-management-browser-') as directory:
        root = Path(directory)
        target = Target(root / 'private/settings.json', root / 'state.sqlite3', 'rdp-browser-test.service', root)
        calls = []
        status_gate = threading.Event()
        def service(_target, action='status'):
            if action == 'status':
                status_gate.wait(timeout=10)
                return dict(LoadState='loaded', ActiveState='active', SubState='running')
            calls.append(action)
            return dict(message='已执行模拟服务操作：' + action, output='模拟日志，无真实部署数据。')
        with patch('management.service_operation', service), patch('management_web.service_operation', service):
            server = create_server(target, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    chrome = shutil.which('google-chrome')
                    browser = playwright.chromium.launch(**({'executable_path': chrome} if chrome else {}))
                    try:
                        page = browser.new_page(viewport=dict(width=1440, height=1100), reduced_motion='reduce', color_scheme='dark')
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.on('console', lambda msg: errors.append(msg.text) if msg.type == 'error' and 'Content Security Policy' in msg.text else None)
                        page.goto(server.management_url)
                        expect(page.locator('#loading-status')).to_be_visible()
                        expect(page.locator('#refresh')).to_be_disabled()
                        expect(page.locator('#runtime-stats')).to_have_attribute('aria-busy', 'true')
                        assert page.locator('.spin').evaluate('(el) => getComputedStyle(el).animationName') == 'none'
                        status_gate.set()
                        expect(page.locator('#config-state')).to_have_text('待配置')
                        expect(page.locator('#loading-status')).to_be_hidden()
                        expect(page.locator('#profile-label')).to_have_text('项目配置')
                        expect(page.locator('#migration-card')).to_be_hidden()
                        assert '#token' not in page.url
                        expect(page.locator('[data-service=restart]')).to_be_disabled()
                        # Failed reads always release the loading state and allow a retry.
                        page.route('**/api/config', lambda route: route.abort())
                        page.locator('#refresh').click()
                        expect(page.locator('#notice')).to_contain_text('无法连接')
                        expect(page.locator('#loading-status')).to_be_hidden()
                        expect(page.locator('#refresh')).to_be_enabled()
                        page.unroute('**/api/config')
                        page.locator('#refresh').click()
                        expect(page.locator('#refresh')).to_be_enabled()
                        # Saved themes survive reload; icons expose the next action by name.
                        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
                        page.get_by_role('button', name='切换浅色主题').click()
                        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
                        page.reload()
                        expect(page.get_by_role('button', name='切换深色主题')).to_be_visible()
                        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
                        page.get_by_role('button', name='切换深色主题').click()
                        # Anchor navigation keeps the page and positions headings below the header.
                        page.locator('.desktop-nav a[href="#configuration"]').click()
                        expect(page.locator('.desktop-nav a[href="#configuration"]')).to_have_attribute('aria-current', 'location')
                        assert page.locator('#configuration').bounding_box()['y'] >= 95
                        # At the page bottom the final section may not reach the top of a tall viewport.
                        page.locator('.desktop-nav a[href="#maintenance"]').click()
                        expect(page.locator('.desktop-nav a[href="#maintenance"]')).to_have_attribute('aria-current', 'location')
                        page.set_viewport_size(dict(width=390, height=844))
                        expect(page.locator('#mobile-nav')).to_be_hidden()
                        assert page.locator('#mobile-nav').evaluate('(el) => el.inert')
                        page.locator('#menu-toggle').click()
                        expect(page.locator('#mobile-nav')).to_be_visible()
                        page.keyboard.press('Escape')
                        expect(page.locator('#menu-toggle')).to_be_focused()
                        expect(page.locator('#mobile-nav')).to_be_hidden()
                        page.locator('#menu-toggle').click()
                        page.locator('#mobile-nav a[href="#maintenance"]').click()
                        expect(page.locator('#mobile-nav')).to_be_hidden()
                        expect(page.locator('#maintenance')).to_be_focused()
                        assert page.locator('#maintenance').bounding_box()['y'] >= 95
                        page.set_viewport_size(dict(width=1440, height=1100))
                        page.locator('#hostname').fill('auth.example.test')
                        page.locator('#rdp_address').fill('desktop.example.test:3389')
                        page.locator('#tunnel_id').fill('123')
                        page.locator('#sakura_token').fill('browser-test-token')
                        password = 'Browser-test-password-1234!'
                        page.locator('#password').fill(password)
                        page.locator('[data-reveal=password]').click()
                        expect(page.locator('#password')).to_have_attribute('type', 'text')
                        expect(page.locator('[data-reveal=password]')).to_have_attribute('aria-label', '隐藏固定访问密码')
                        page.locator('[data-reveal=password]').click()
                        expect(page.locator('#password')).to_have_attribute('type', 'password')
                        page.locator('#password-confirm').fill(password + 'x')
                        page.locator('#save').click()
                        expect(page.locator('#notice')).to_contain_text('不一致')
                        assert not target.config.exists()
                        page.locator('#password-confirm').fill(password)
                        page.locator('#turnstile-enabled').check()
                        page.locator('#turnstile_site_key').fill('site-for-browser-test')
                        page.locator('#turnstile_secret_key').fill('secret-for-browser-test')
                        page.locator('#save').click()
                        expect(page.locator('#config-state')).to_have_text('已配置')
                        expect(page.locator('#notice')).to_contain_text('已创建')
                        expect(page.locator('#password')).to_have_value('')
                        expect(page.locator('#password-editor')).to_be_hidden()
                        expect(page.locator('#password-saved')).to_contain_text('固定密码已设置')
                        expect(page.locator('#sakura_token')).to_have_value('browser-test-token')
                        expect(page.locator('#turnstile_secret_key')).to_have_value('secret-for-browser-test')
                        expect(page.locator('#sakura_token')).to_have_attribute('type', 'password')
                        page.locator('[data-reveal=sakura_token]').click()
                        expect(page.locator('#sakura_token')).to_have_attribute('type', 'text')
                        page.locator('[data-reveal=turnstile_secret_key]').click()
                        expect(page.locator('#turnstile_secret_key')).to_have_attribute('type', 'text')
                        page.reload()
                        expect(page.locator('#sakura_token')).to_have_value('browser-test-token')
                        expect(page.locator('#sakura_token')).to_have_attribute('type', 'password')
                        expect(page.locator('#turnstile_secret_key')).to_have_value('secret-for-browser-test')
                        expect(page.locator('#turnstile_secret_key')).to_have_attribute('type', 'password')
                        before, _ = read_config(target.config)
                        page.locator('#rdp_address').fill('new.example.test:3390')
                        # Cancelling a password replacement preserves other unsaved edits.
                        page.locator('#password-change').click()
                        page.locator('#password').fill('Cancelled-password-1234!')
                        page.locator('#password-cancel').click()
                        expect(page.locator('#password-editor')).to_be_hidden()
                        expect(page.locator('#password')).to_have_value('')
                        expect(page.locator('#save-hint')).to_contain_text('尚未保存')
                        submitted = []
                        page.on('request', lambda request: submitted.append(request.post_data_json)
                                if request.method == 'POST' and request.url.endswith('/api/config') else None)
                        page.locator('#save').click()
                        expect(page.locator('#notice')).to_contain_text('已保存')
                        after, _ = read_config(target.config)
                        for key in ['session_key', 'password_hash', 'sakura_token', 'turnstile_secret_key']:
                            assert after[key] == before[key]
                        for key in ('password', 'sakura_token', 'turnstile_secret_key'):
                            assert key not in submitted[-1]['changes']
                        assert Path(str(target.config) + '.bak').is_file()
                        # A deliberate password change closes the editor and retains both tokens.
                        page.locator('#password-change').click()
                        replacement = 'Changed-browser-password-4567!'
                        page.locator('#password').fill(replacement)
                        page.locator('#password-confirm').fill(replacement)
                        page.locator('#save').click()
                        expect(page.locator('#password-editor')).to_be_hidden()
                        expect(page.locator('#password')).to_have_value('')
                        changed, _ = read_config(target.config)
                        assert changed['password_hash'] != before['password_hash']
                        for key in ('session_key', 'sakura_token', 'turnstile_secret_key'):
                            assert changed[key] == before[key]

                        # Another writer changes the disk after this form was loaded.
                        save_config(target, {'tunnel_id': 456})
                        page.locator('#rdp_address').fill('stale.example.test:3391')
                        page.locator('#save').click()
                        expect(page.locator('#notice')).to_contain_text('其他窗口')
                        assert read_config(target.config)[0]['tunnel_id'] == 456
                        page.locator('#refresh').click()
                        expect(page.locator('#confirm-dialog')).to_be_visible()
                        page.locator('#confirm-cancel').click()
                        expect(page.locator('#rdp_address')).to_have_value('stale.example.test:3391')
                        page.locator('#refresh').click()
                        page.locator('#confirm-ok').click()
                        expect(page.locator('#tunnel_id')).to_have_value('456')
                        page.locator('#turnstile-enabled').uncheck()
                        page.locator('#save').click()
                        expect(page.locator('#notice')).to_contain_text('已保存')
                        assert read_config(target.config)[0]['turnstile_secret_key'] == ''

                        # Unlock a disposable state database through the actual HTTP API.
                        AuthGuard(target.state)
                        with closing(sqlite3.connect(target.state)) as db, db:
                            db.execute('CREATE TABLE passkeys (id TEXT PRIMARY KEY)')
                            db.execute("INSERT INTO passkeys VALUES ('keep-passkey')")
                            db.execute('CREATE TABLE temporary_password (id INTEGER PRIMARY KEY, generation INTEGER)')
                            db.execute('INSERT INTO temporary_password VALUES (1, 5)')
                            db.execute('UPDATE guard_global SET until=?', (int(time.time()) + 900,))
                        page.locator('#refresh').click()
                        expect(page.locator('#unlock')).to_be_enabled()
                        page.locator('#unlock').click()
                        page.locator('#confirm-ok').click()
                        expect(page.locator('#guard-state')).to_have_text('保护正常')
                        expect(page.locator('#temporary-generation')).to_have_text('5')
                        expect(page.locator('#passkey-count')).to_have_text('1')

                        # Updating status must not silently replace a stale form revision.
                        save_config(target, {'tunnel_id': 789})
                        page.locator('#rdp_address').fill('another-stale.example.test:3392')
                        page.locator('#unlock').click()
                        page.locator('#confirm-ok').click()
                        expect(page.locator('#notice')).to_contain_text('已解除')
                        page.locator('#save').click()
                        expect(page.locator('#notice')).to_contain_text('其他窗口')
                        assert read_config(target.config)[0]['tunnel_id'] == 789
                        page.locator('#refresh').click()
                        page.locator('#confirm-ok').click()
                        expect(page.locator('#tunnel_id')).to_have_value('789')

                        for theme in ['dark', 'light']:
                            page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                            for width in [320, 390, 768, 1024, 1440]:
                                page.set_viewport_size(dict(width=width, height=1100))
                                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width)
                                expect(page.locator('#save')).to_be_visible()
                        page.set_viewport_size(dict(width=1440, height=1100))
                        page.evaluate("document.documentElement.dataset.theme = 'dark'")
                        page.screenshot(path='/tmp/rdp-management-desktop.png', full_page=True)
                        page.set_viewport_size(dict(width=390, height=844))
                        page.screenshot(path='/tmp/rdp-management-mobile.png', full_page=True)
                        page.reload()
                        expect(page.locator('#config-state')).to_have_text('已配置')
                        assert not errors, errors

                        # An explicit deployment target enables controls, but this backend is a stub.
                        system_target = Target(target.config, target.state, target.service, target.runtime, 'system')
                        system_server = create_server(system_target, port=0)
                        system_thread = threading.Thread(target=system_server.serve_forever, daemon=True)
                        system_thread.start()
                        try:
                            page.goto(system_server.management_url)
                            expect(page.locator('[data-service=restart]')).to_be_enabled()
                            page.locator('[data-service=restart]').click()
                            page.locator('#confirm-cancel').click()
                            assert calls == []
                            page.locator('[data-service=restart]').click()
                            page.locator('#confirm-ok').click()
                            expect(page.locator('#notice')).to_contain_text('模拟服务操作')
                            assert calls == ['restart']
                            page.locator('#show-logs').click()
                            expect(page.locator('#logs')).to_contain_text('模拟日志')
                        finally:
                            system_server.shutdown()
                            system_server.server_close()
                            system_thread.join(timeout=5)
                    finally:
                        status_gate.set()
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
    print('管理页浏览器检查通过：官网导航/移动菜单、加载与失败重试、主题持久化、图标操作、配置与凭据保留、冲突保护、服务维护、减少动态效果和 320～1440px 布局。')


if __name__ == '__main__':
    main()
