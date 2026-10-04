"""Browser test of the deployment wizard; no real services, credentials or downloads."""
from pathlib import Path
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from management import Target
from management_web import create_server
from test_deploy import DeploymentTests


def main():
    fixture = DeploymentTests()
    fixture.setUp()
    server = None
    try:
        deployment = fixture.deployment
        target = Target(deployment.stage / 'draft.json', deployment.stage / 'state.sqlite3',
                        'rdp-access-auth.service', deployment.stage)
        server = create_server(target, port=0, deployment=deployment)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with sync_playwright() as playwright:
            chrome = shutil.which('google-chrome')
            browser = playwright.chromium.launch(**({'executable_path': chrome} if chrome else {}))
            try:
                page = browser.new_page(viewport=dict(width=1440, height=1100))
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(server.management_url)
                expect(page.locator('#phase')).to_have_text('等待填写信息')
                page.locator('#hostname').fill('auth.example.test')
                page.locator('#rdp-address').fill('desktop.example.test:3389')
                page.locator('#tunnel-id').fill('12')
                page.locator('#sakura-token').fill('fixture-sakura-token')
                page.locator('#password').fill('Fixture-browser-password-1234!')
                page.locator('#password-confirm').fill('Wrong-password-1234!')
                page.locator('#cloudflare-token').fill('fixture-cloudflare-token')
                page.locator('#prerequisites').check()
                page.locator('#prepare').click()
                expect(page.locator('#notice')).to_contain_text('不一致')
                assert not fixture.layout.config_dir.exists()
                page.locator('#password-confirm').fill('Fixture-browser-password-1234!')
                page.locator('#prepare').click()
                expect(page.locator('#phase')).to_have_text('等待确认')
                expect(page.locator('#plan')).to_contain_text('auth.example.test')
                expect(page.locator('#cloudflare-token')).to_have_value('')
                assert not fixture.layout.config_dir.exists()
                assert fixture.commands == []
                page.reload()
                expect(page.locator('#phase')).to_have_text('等待确认')
                for theme in ['dark', 'light']:
                    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                    for width in [320, 390, 768, 1440]:
                        page.set_viewport_size(dict(width=width, height=1100))
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                page.set_viewport_size(dict(width=1440, height=1100))
                page.screenshot(path='/tmp/rdp-deployment-wizard.png', full_page=True)
                page.locator('#install').click()
                expect(page.locator('#phase')).to_have_text('部署完成')
                expect(page.locator('#site-url')).to_have_text('https://auth.example.test')
                assert fixture.layout.config.exists()
                assert fixture.commands[0] == ['systemctl', 'daemon-reload']
                assert not errors, errors
                # Starting a new wizard against that layout must show a conflict before any input.
                deployment.phase = 'idle'
                page.reload()
                expect(page.locator('#notice')).to_contain_text('已有部署')
                expect(page.locator('#prepare')).to_be_disabled()
            finally:
                browser.close()
    finally:
        if server:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        fixture.doCleanups()
    print('部署向导浏览器回归通过：输入校验、准备/确认隔离、密钥清空、刷新恢复、模拟部署、已有部署保护、深浅主题与窄屏布局。')


if __name__ == '__main__':
    main()
