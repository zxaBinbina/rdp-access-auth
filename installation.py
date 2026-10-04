"""Detect installed deployments without reading or creating credentials."""
from pathlib import Path
import os

from management import Target


def installed_targets(root=Path('/')):
    root = Path(root)
    found = []
    for profile, name, runtime in (
            ('system', 'rdp-access-auth', '/opt/rdp-access-auth'),
            ('legacy', 'rdp-auth', '/usr/local/lib/rdp-auth')):
        config_dir = root / 'etc' / name
        unit = name + '.service'
        markers = [config_dir, root / 'var/lib' / name, root / runtime.lstrip('/')]
        markers += [root / directory / unit for directory in ('etc/systemd/system', 'usr/lib/systemd/system', 'lib/systemd/system')]
        if any(os.path.lexists(path) for path in markers):
            found.append(Target(config_dir / 'portal-settings.json', root / 'var/lib' / name / 'state.sqlite3',
                                unit, root / runtime.lstrip('/'), profile))
    return found


def preferred_target(targets, probe):
    """Prefer the running deployment when both old and standard layouts exist."""
    if len(targets) < 2:
        return targets[0] if targets else None
    for target in targets:
        if probe(target).get('ActiveState') == 'active':
            return target
    return targets[0]
