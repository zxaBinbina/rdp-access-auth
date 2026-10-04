"""Check the migration UI against disposable data, including interrupted recovery."""
from pathlib import Path
from dataclasses import replace
import json
import shutil
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from management_web import create_server
from management import ManagementError
from migration import Migration
from test_migration import MigrationTests


def main():
    fixture = MigrationTests()
    fixture.setUp()
    def service(target, action='status'):
        return fixture.probe(target) if action == 'status' else dict(message='模拟操作')
    with patch('management.service_operation', service):
        server = create_server(fixture.target, port=0, migration=fixture.migration)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                chrome = shutil.which('google-chrome')
                browser = p.chromium.launch(**({'executable_path': chrome} if chrome else {}))
                try:
                    page = browser.new_page(viewport=dict(width=1440, height=1100))
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(server.management_url.replace('/#token=', '/?view=migrate#token='))
                    expect(page.locator('#migration-apply')).to_be_visible()
                    expect(page.locator('#profile-label')).to_have_text('旧版部署')
                    expect(page.locator('#migration-check')).to_be_focused()
                    expect(page.locator('#migration-service')).to_contain_text('rdp-auth.service')
                    expect(page.locator('#local-instructions')).to_be_hidden()
                    assert page.locator('.brand-icon').evaluate('(img) => img.complete && img.naturalWidth > 0')
                    page.locator('#migration-apply').click()
                    expect(page.locator('#confirm-dialog')).to_be_visible()
                    page.locator('#confirm-cancel').click()
                    assert fixture.commands == []
                    for theme in ('light', 'dark'):
                        page.evaluate('(value) => document.documentElement.dataset.theme=value', theme)
                        for width in (320, 390, 768, 1440):
                            page.set_viewport_size(dict(width=width, height=1100))
                            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width)
                    page.screenshot(path='/tmp/rdp-migration-management.png', full_page=True)
                    # A failed cutover must remain on a usable page and survive reload.
                    original_health = fixture.migration.health
                    def failing_health(*args):
                        if fixture.migration.dropin.exists():
                            raise ManagementError('模拟凭据加载失败')
                    fixture.migration.health = failing_health
                    page.locator('#migration-apply').click()
                    page.locator('#confirm-ok').click()
                    expect(page.locator('#migration-error')).to_contain_text('已恢复原程序', timeout=15000)
                    page.reload()
                    expect(page.locator('#migration-error')).to_contain_text('模拟凭据加载失败')
                    expect(page.locator('#migration-card')).to_be_visible()
                    expect(page.locator('#profile-label')).to_have_text('旧版部署')
                    expect(page.locator('#save')).to_be_enabled()
                    assert browser.is_connected()
                    fixture.assert_credentials()
                    fixture.migration.health = original_health
                    page.locator('#migration-apply').click()
                    page.locator('#confirm-ok').click()
                    expect(page.locator('#profile-label')).to_have_text('软件包部署', timeout=15000)
                    expect(page.locator('#migration-card')).to_be_hidden()
                    fixture.assert_credentials()
                    assert fixture.migration.record()['phase'] == 'complete'
                    page.reload()
                    expect(page.locator('#profile-label')).to_have_text('软件包部署')
                    expect(page.locator('#migration-card')).to_be_hidden()
                    page.locator('#refresh').click()
                    expect(page.locator('#refresh')).to_be_enabled()
                    expect(page.locator('#profile-label')).to_have_text('软件包部署')
                    expect(page.locator('#migration-card')).to_be_hidden()
                    # Tutorial links must not reveal or focus an obsolete migration entry.
                    page.goto(server.management_url.replace('/#token=', '/?view=migrate#token='))
                    expect(page.locator('#notice')).to_contain_text('无需迁移')
                    expect(page.locator('#migration-card')).to_be_hidden()
                    expect(page.locator('#migration-check')).not_to_be_focused()
                    # Fresh package installations use the system profile without a migration receipt.
                    fresh_target = replace(fixture.target, profile='system', service='rdp-access-auth.service',
                                           runtime=fixture.root / 'opt/rdp-access-auth',
                                           config=fixture.root / 'etc/rdp-access-auth/portal-settings.json')
                    fresh_target.config.parent.mkdir(parents=True)
                    fresh_target.config.write_bytes(fixture.before)
                    fresh_migration = Migration(fresh_target, root=fixture.root, check_host=False, probe=fixture.probe)
                    fresh_server = create_server(fresh_target, port=0, migration=fresh_migration)
                    fresh_thread = threading.Thread(target=fresh_server.serve_forever, daemon=True)
                    fresh_thread.start()
                    try:
                        page.goto(fresh_server.management_url)
                        expect(page.locator('#profile-label')).to_have_text('软件包部署')
                        expect(page.locator('#migration-card')).to_be_hidden()
                        assert not fresh_migration.receipt.exists()
                    finally:
                        fresh_server.shutdown()
                        fresh_server.server_close()
                        fresh_thread.join(timeout=5)
                        fresh_migration.close()
                    # Simulate an interrupted switch and verify recovery survives reload.
                    record = fixture.migration.record()
                    fixture.migration._write_record(record, 'switching')
                    page.goto(server.management_url)
                    expect(page.locator('#migration-recover')).to_be_visible()
                    expect(page.locator('#save')).to_be_disabled()
                    page.locator('#migration-recover').click()
                    page.locator('#confirm-ok').click()
                    expect(page.locator('#migration-apply')).to_be_visible(timeout=15000)
                    expect(page.locator('#profile-label')).to_have_text('旧版部署')
                    assert fixture.migration.record()['phase'] == 'rolled-back'
                    fixture.assert_credentials()
                    assert not errors, errors
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            fixture.doCleanups()
    print('迁移管理页通过：旧版识别、迁移后标识与入口隐藏、刷新/重开、新装软件包、URL 导航、失败回滚、中断恢复、凭据保留和深浅主题/窄屏。')


if __name__ == '__main__':
    main()
