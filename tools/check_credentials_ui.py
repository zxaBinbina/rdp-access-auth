"""Browser checks backed by Flask test sessions; no external authorization."""
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from test_portal import PortalTests


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
                assert not errors, errors
                print('Credential browser checks passed: regeneration, fixed-password binding, narrow layouts.')
            finally:
                browser.close()
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
