"""Browser checks backed by Flask test sessions; no external authorization."""
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from test_portal import PortalTests
from portal import create_app


def main():
    fixture = PortalTests()
    fixture.setUp()
    try:
        with sync_playwright() as playwright:
            chrome = shutil.which('google-chrome')
            browser = playwright.chromium.launch(**({'executable_path': chrome} if chrome else {}))
            try:
                page = browser.new_page(reduced_motion='reduce')
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))

                def route(intercept):
                    req = intercept.request
                    url = urlsplit(req.url)
                    response = fixture.client.open(url.path + ('?' + url.query if url.query else ''),
                        method=req.method, base_url=fixture.base, headers=fixture.headers,
                        data=req.post_data, content_type=req.headers.get('content-type'))
                    headers = dict(response.headers)
                    headers.pop('Set-Cookie', None)  # Test client owns the signed session.
                    intercept.fulfill(status=response.status_code, headers=headers, body=response.data)

                page.route(fixture.base + '/**', route)
                page.goto(fixture.base + '/?method=temporary')
                for i, word in enumerate(fixture.initial.split('-'), 1):
                    page.locator(f'#temporary-{i}').fill(word)
                page.locator('.authorize-button').click()
                expect(page.locator('#regenerate-temporary')).to_be_visible()
                previous = fixture.temp.current()
                for width in (320, 390, 1440):
                    page.set_viewport_size(dict(width=width, height=1000))
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#regenerate-temporary').click()
                expect(page).to_have_url(fixture.base + '/authorized')
                expect(page.locator('#next-temporary')).not_to_have_text(previous)
                expect(page.locator('#next-temporary')).to_have_text(fixture.temp.current())
                page.get_by_role('link', name='绑定通行密钥 / 查看当前临时密码').click()
                expect(page.locator('#register-key')).to_have_count(0)
                page.get_by_role('link', name='使用固定密码认证', exact=True).click()
                page.locator('#password').fill('Correct-password-1234')
                page.locator('.authorize-button').click()
                page.get_by_role('link', name='绑定通行密钥 / 查看当前临时密码').click()
                expect(page.locator('#register-key')).to_be_visible()
                # New browser sessions on an admitted IP see revocation, never
                # credential-management privileges. Route into the real Flask API.
                fixture.settings['local_admission'] = {}
                fixture.app = create_app(fixture.settings, fixture.state, fixture.grants.append)
                fixture.client = fixture.app.test_client()
                store = fixture.app.extensions['admissions']
                store.grant('1.1.1.1')
                original_route = route

                def local_route(intercept):
                    store.heartbeat()  # Simulated gateway health for this UI-only test.
                    original_route(intercept)

                page.unroute(fixture.base + '/**', route)
                page.route(fixture.base + '/**', local_route)
                page.goto(fixture.base + '/')
                expect(page.locator('#revoke-admission')).to_be_visible()
                expect(page.locator('#auth-form')).to_have_count(0)
                expect(page.locator('a[href="/credentials"]')).to_have_count(0)
                for width in (320, 390, 1440):
                    page.set_viewport_size(dict(width=width, height=1000))
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.get_by_role('link', name='使用固定密码管理凭据').click()
                expect(page.locator('#auth-form')).to_be_visible()
                page.goto(fixture.base + '/')
                page.locator('#revoke-admission').click()
                expect(page.locator('#auth-form')).to_be_visible()
                assert store.get('1.1.1.1') is None
                store.grant('8.8.8.8')
                page.goto(fixture.base + '/?ipv4=8.8.8.8')
                expect(page.locator('#auth-form')).to_be_visible()
                expect(page.locator('#current-ip')).to_have_text('1.1.1.1')
                expect(page.locator('[name=ipv4]')).to_have_count(0)
                expect(page.locator('#revoke-admission')).to_have_count(0)
                assert not errors, errors
                print('Credential browser checks passed: regeneration, fixed-password binding, IP admission/revocation, narrow layouts.')
            finally:
                browser.close()
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
