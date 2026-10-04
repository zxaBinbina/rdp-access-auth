"""Strict, read-only navigation links for the desktop protocol handler."""
from urllib.parse import parse_qs, urlsplit

from management import ManagementError

ROUTES = {'open', 'manage', 'deploy', 'migrate', 'logs'}
PROFILES = {'auto', 'legacy', 'system'}


def parse_app_link(value):
    if not isinstance(value, str) or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ManagementError('助手链接格式不正确。')
    try:
        link = urlsplit(value)
        query = parse_qs(link.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ManagementError('助手链接格式不正确。') from None
    route = link.netloc.lower()
    if (link.scheme != 'rdp-auth' or route not in ROUTES or link.path not in ('', '/') or
            link.fragment or set(query) - {'profile'}):
        raise ManagementError('不支持此助手链接。可使用 rdp-auth://manage、rdp-auth://migrate 或 rdp-auth://logs。')
    profile = query.get('profile', ['auto'])
    if len(profile) != 1 or profile[0] not in PROFILES:
        raise ManagementError('链接中的 profile 只能是 auto、system 或 legacy。')
    return dict(view=route, profile=profile[0])
