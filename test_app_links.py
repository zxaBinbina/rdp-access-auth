import unittest
from unittest.mock import patch

from app_links import parse_app_link
from management import ManagementError, select_target
from rdp_manager import main


class AppLinksTests(unittest.TestCase):
    def test_routes_and_profiles(self):
        for view in ('open', 'manage', 'deploy', 'migrate', 'logs'):
            self.assertEqual(parse_app_link('rdp-auth://' + view), dict(view=view, profile='auto'))
            self.assertEqual(parse_app_link(f'rdp-auth://{view}/?profile=legacy'), dict(view=view, profile='legacy'))

    def test_links_cannot_inject_actions_or_sensitive_parameters(self):
        for value in ('https://manage', 'rdp-auth://migrate?apply=true', 'rdp-auth://manage?token=secret',
                      'rdp-auth://manage?profile=system&profile=legacy', 'rdp-auth://manage?config=/etc/passwd',
                      'rdp-auth://user@manage', 'rdp-auth://manage:18124', 'rdp-auth://manage/../migrate',
                      'rdp-auth://manage#execute', 'rdp-auth://restart', 'rdp-auth://manage?profile=',
                      'rdp-auth://manage\nanything', 'rdp-auth://manage?profile=local'):
            with self.subTest(value=value), self.assertRaises(ManagementError):
                parse_app_link(value)

    def test_migration_link_opens_page_without_starting_migration(self):
        target = select_target('legacy')
        with patch('deploy.elevate_if_needed'), patch('installation.installed_targets', return_value=[target]), \
             patch('management_web.run_gui') as gui, patch('migration.Migration.start') as migrate:
            self.assertEqual(main(['launch', 'rdp-auth://migrate?profile=legacy']), 0)
            gui.assert_called_once_with(target, 18124, True, view='migrate', fallback_port=True)
            migrate.assert_not_called()

    def test_link_on_fresh_host_opens_first_deployment(self):
        with patch('deploy.elevate_if_needed'), patch('installation.installed_targets', return_value=[]), \
             patch('deploy.run_wizard') as wizard, patch('management_web.run_gui') as gui:
            self.assertEqual(main(['launch', 'rdp-auth://manage?profile=legacy']), 0)
            wizard.assert_called_once_with(True, 18124, False)
            gui.assert_not_called()


if __name__ == '__main__':
    unittest.main()
