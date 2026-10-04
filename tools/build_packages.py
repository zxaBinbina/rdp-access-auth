"""Build native packages from an isolated, dependency-only build virtualenv."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
APP_FILES = ('portal.py', 'portal.html', 'auth_credentials.py', 'auth_guard.py', 'management.py',
             'management_web.py', 'rdp_manager.py', 'package_bootstrap.py', 'deploy.py', 'VERSION',
             'sakura_config.py', 'installation.py', 'migration.py', 'app_links.py', 'LICENSE', 'AUTHORS',
             'readme.md', 'requirements.txt', 'requirements-runtime.txt')
REMOVE_SERVICES = '''if [ -f /etc/rdp-access-auth/deployment.json ] && command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now cloudflared-rdp-access.service rdp-access-auth.service >/dev/null 2>&1 || :
fi
for unit in rdp-auth.service rdp-access-auth.service; do
    if [ -f "/etc/systemd/system/$unit.d/90-rdp-access-auth-package.conf" ]; then
        systemctl stop "$unit" >/dev/null 2>&1 || :
    fi
done
'''


def stage_tree(destination):
    app = destination / 'usr/lib/rdp-access-auth'
    app.mkdir(parents=True)
    for name in APP_FILES:
        shutil.copyfile(ROOT / name, app / name)
    for name in ('admin_ui',):
        shutil.copytree(ROOT / name, app / name)
    for name in ('tools/build_wordlist.py', 'tools/preview.py', 'wordlists/sources.json', 'wordlists/readme.md',
                 'deployment/cloudflared-downloads.json'):
        (app / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, app / name)
    licenses = destination / 'usr/share/licenses/rdp-access-auth'
    licenses.mkdir(parents=True)
    shutil.copyfile(ROOT / 'LICENSE', licenses / 'LICENSE')
    shutil.copyfile(ROOT / 'admin_ui/icons.LICENSE', licenses / 'Lucide-ISC')
    docs = destination / 'usr/share/doc/rdp-access-auth'
    docs.mkdir(parents=True)
    for name in ('AUTHORS', 'readme.md'):
        shutil.copyfile(ROOT / name, docs / name)
    library = Path(sysconfig.get_path('purelib'))
    vendor = app / 'vendor'
    vendor.mkdir()
    # Copy from distribution manifests, preserving wheel license files while excluding entry-point scripts.
    included = []
    for distribution in importlib.metadata.distributions(path=[str(library)]):
        if distribution.metadata['Name'].lower() in ('pip', 'setuptools', 'wheel'):
            continue
        name = distribution.metadata['Name']
        legacy_licenses = {'blinker': 'MIT', 'itsdangerous': 'BSD-3-Clause', 'jinja2': 'BSD-3-Clause',
                           'pyasn1': 'BSD-2-Clause', 'pyasn1_modules': 'BSD-2-Clause',
                           'pyopenssl': 'Apache-2.0', 'gunicorn': 'MIT'}
        license_expression = distribution.metadata.get('License-Expression') or legacy_licenses.get(name.lower())
        if not license_expression:
            raise RuntimeError('请核对依赖许可证：' + name)
        included.append(dict(name=name, version=distribution.version, license=license_expression))
        for relative in distribution.files or []:
            if '..' in relative.parts or '__pycache__' in relative.parts or relative.suffix == '.pyc':
                continue
            source = library / relative
            if source.is_file():
                output = vendor / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, output)
                if any(part.lower().startswith(('license', 'copying', 'notice')) for part in relative.parts):
                    license_file = licenses / 'third-party' / name / relative
                    license_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, license_file)
    required = {'flask', 'gunicorn', 'cryptography', 'webauthn', 'cbor2'}
    if not required <= {entry['name'].lower() for entry in included}:
        raise RuntimeError('构建环境缺少运行依赖，请使用 tools/build-package。')
    (app / 'THIRD_PARTY.json').write_text(json.dumps(included, indent=2) + '\n')
    (app / 'PACKAGED.json').write_text(json.dumps(dict(python=f'{sys.version_info.major}.{sys.version_info.minor}',
                                                       arch=platform.machine(), version=(ROOT / 'VERSION').read_text().strip())) + '\n')
    for source, name, mode in [('rdp-auth', 'usr/bin/rdp-auth', 0o755),
                               ('rdp-access-auth.desktop', 'usr/share/applications/rdp-access-auth.desktop', 0o644),
                               ('rdp-access-auth.png', 'usr/share/icons/hicolor/512x512/apps/rdp-access-auth.png', 0o644),
                               ('cc.cd.zxabinbina.RDPAccessAuth.metainfo.xml', 'usr/share/metainfo/cc.cd.zxabinbina.RDPAccessAuth.metainfo.xml', 0o644)]:
        output = destination / name
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / 'packaging' / source, output)
        output.chmod(mode)
    # No configuration, credentials, database, wordlist data or enable-on-install script is packaged.
    for path in destination.rglob('*'):
        if path.is_dir():
            path.chmod(0o755)
        elif path != destination / 'usr/bin/rdp-auth':
            path.chmod(0o644)


def build_deb(tree, output, version):
    minor = sys.version_info.minor
    architecture = {'x86_64': 'amd64', 'aarch64': 'arm64'}[platform.machine()]
    control = tree / 'DEBIAN'
    control.mkdir()
    (control / 'control').write_text(f'''Package: rdp-access-auth
Version: {version}
Section: net
Priority: optional
Architecture: {architecture}
Maintainer: a彬彬a <zxabinbina@mcyzw.top>
Homepage: https://github.com/zxaBinbina/rdp-access-auth
Depends: python3 (>= 3.{minor}), python3 (<< 3.{minor + 1}), libc6 (>= 2.34), libgcc-s1, systemd (>= 249), sudo
Recommends: xdg-utils, x-terminal-emulator, desktop-file-utils
Description: Browser and command-line setup for RDP access authentication
 Includes runtime Python libraries and an interactive deployment wizard.
 Credentials and services are created only when the user runs the wizard.
''')
    (control / 'prerm').write_text('#!/bin/sh\nset -e\nif [ "$1" = remove ]; then\n' + REMOVE_SERVICES + 'fi\nexit 0\n')
    (control / 'prerm').chmod(0o755)
    artifact = output / f'rdp-access-auth_{version}_py3{minor}_{architecture}.deb'
    subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(tree), str(artifact)], check=True)
    return artifact


def build_rpm(tree, output, version, work):
    minor = sys.version_info.minor
    libraries = json.loads((tree / 'usr/lib/rdp-access-auth/THIRD_PARTY.json').read_text())
    license_expression = ' AND '.join(sorted({'MIT', 'ISC', *(f'({item["license"]})' if ' OR ' in item['license'] else item['license'] for item in libraries)}))
    bundled = '\n'.join(f'Provides: bundled(python3dist({item["name"].lower().replace("_", "-")})) = {item["version"]}' for item in libraries)
    for folder in ('SOURCES', 'SPECS', 'BUILD', 'BUILDROOT', 'RPMS', 'SRPMS'):
        (work / folder).mkdir()
    with tarfile.open(work / 'SOURCES/app.tar.gz', 'w:gz') as archive:
        archive.add(tree / 'usr', arcname='usr')
    spec = work / 'SPECS/rdp-access-auth.spec'
    spec.write_text(f'''%global debug_package %{{nil}}
%global __os_install_post %{{nil}}
%global __provides_exclude_from ^/usr/lib/rdp-access-auth/vendor/.*$
Name: rdp-access-auth
Version: {version}
Release: 1%{{?dist}}
Summary: RDP Access Auth - Remote desktop authentication manager
Summary(zh_CN): RDP Access Auth - 远程桌面认证管理助手
License: {license_expression}
URL: https://github.com/zxaBinbina/rdp-access-auth
Vendor: a彬彬a
Packager: a彬彬a <zxabinbina@mcyzw.top>
Source0: app.tar.gz
{bundled}
Requires: python3
Requires: python(abi) = 3.{minor}
Requires: systemd >= 249
Requires: sudo
Recommends: xdg-utils
Recommends: desktop-file-utils

%description
RDP Access Auth by a彬彬a. An MIT-licensed authentication portal with
automatic deployment detection, local management and recoverable migration.
Bundled libraries retain their respective licenses.
Installing this package does not configure or enable any service.

%prep
%setup -q -c -T
tar -xzf %{{SOURCE0}}

%build

%install
mkdir -p %{{buildroot}}
cp -a usr %{{buildroot}}/

%preun
if [ "$1" -eq 0 ]; then
{REMOVE_SERVICES}fi

%files
%defattr(-,root,root,-)
/usr/bin/rdp-auth
/usr/lib/rdp-access-auth
/usr/share/applications/rdp-access-auth.desktop
/usr/share/icons/hicolor/512x512/apps/rdp-access-auth.png
/usr/share/metainfo/cc.cd.zxabinbina.RDPAccessAuth.metainfo.xml
%license /usr/share/licenses/rdp-access-auth
%doc /usr/share/doc/rdp-access-auth
''')
    result = subprocess.run(['rpmbuild', '-bb', '--define', f'_topdir {work}', str(spec)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode:
        print(result.stdout, file=sys.stderr)
        result.check_returncode()
    built = list((work / 'RPMS').rglob('*.rpm'))
    if len(built) != 1:
        raise RuntimeError('RPM 构建未生成唯一安装包。')
    artifact = output / built[0].name
    shutil.copyfile(built[0], artifact)
    return artifact


def main():
    parser = argparse.ArgumentParser(description='构建当前 Linux / Python ABI 的原生安装包')
    parser.add_argument('format', nargs='?', default='all', choices=['all', 'rpm', 'deb'])
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    if sys.prefix == sys.base_prefix:
        parser.error('请使用 tools/build-package，在隔离构建环境中运行。')
    formats = ('deb', 'rpm') if args.format == 'all' else (args.format,)
    for package_format in formats:
        tool = 'rpmbuild' if package_format == 'rpm' else 'dpkg-deb'
        if not shutil.which(tool):
            parser.error('缺少构建工具：' + tool)
    if platform.machine() not in ('x86_64', 'aarch64'):
        parser.error('目前支持 x86_64 和 aarch64。')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    version = (ROOT / 'VERSION').read_text().strip()
    for package_format in formats:
        with tempfile.TemporaryDirectory(prefix='rdp-package-') as temporary:
            work = Path(temporary)
            tree = work / 'tree'
            stage_tree(tree)
            artifact = build_deb(tree, output, version) if package_format == 'deb' else build_rpm(tree, output, version, work)
        checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
        artifact.with_name(artifact.name + '.sha256').write_text(checksum + '  ' + artifact.name + '\n')
        print('安装包：' + str(artifact), flush=True)
    print('构建 Python ABI：' + f'{sys.version_info.major}.{sys.version_info.minor}')


if __name__ == '__main__':
    main()
