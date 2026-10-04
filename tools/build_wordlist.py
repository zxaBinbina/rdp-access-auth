"""Build the Chinese noun pool from separately downloaded public data."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VALID = re.compile(r'[\u4e00-\u9fff]{2,}')

def names(filename, data):
    if filename == 'food.txt':
        values = [line.split('\t')[0].strip() for line in data.decode('utf-8-sig').splitlines()]
    elif filename == 'minecraft-zh_cn.json':
        values = [v for k, v in json.loads(data).items()
                  if re.fullmatch(r'(block|item|entity|enchantment)\.minecraft\.[a-z0-9_]+', k)]
    else:
        values = json.loads(data)
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError('原神数据必须是名称列表')
        values = [v.strip('「」『』“”"') for v in values]
    return {v for v in values if isinstance(v, str) and VALID.fullmatch(v)}

def build_wordlist(output_root=ROOT, download=False, refresh_sources=False, progress=print):
    output_root = Path(output_root)
    manifest_path = ROOT / 'wordlists/sources.json'
    # A refreshed manifest lives with writable data in a native package.
    override = output_root / 'wordlists/sources.json'
    if override.is_file():
        manifest_path = override
    manifest = json.loads(manifest_path.read_text())
    cache = output_root / '.cache/wordlists'
    cache.mkdir(parents=True, exist_ok=True)
    words = set()
    for source in manifest:
        name = source['file']
        if Path(name).name != name or not source['url'].startswith('https://'):
            raise ValueError('不合法的来源路径或网址')
        path = cache / name
        progress('准备词库：' + source['category'])
        if download:
            req = urllib.request.Request(source['url'], headers={'User-Agent': 'rdp-access-auth-wordlist/1.0'})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read(20 * 1024 * 1024 + 1)
            if len(data) > 20 * 1024 * 1024:
                raise ValueError('来源文件超过20MB限制')
        else:
            if not path.exists():
                raise ValueError('缺少数据，请先运行 ./rdp-auth wordlist --download')
            data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != source['sha256'] and not refresh_sources:
            raise ValueError(name + ' 与来源快照不同；确认接受上游更新后使用 wordlist --download --refresh-sources')
        selected = names(name, data)
        if len(selected) < 5:
            raise ValueError('来源数据未包含足够的中文名称：' + name)
        words.update(selected)
        source.update(sha256=digest, entries=len(selected))
        if download:
            path.write_bytes(data)
    if len(words) < 2048:
        raise ValueError('词库必须至少包含2048个不同词')
    output = output_root / 'wordlists/objects.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_suffix('.json.tmp')
    staging.write_text(json.dumps(sorted(words), ensure_ascii=False) + '\n')
    staging.replace(output)
    if refresh_sources:
        override.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    progress(f'构建完成：{len(words)} 个词；不会生成或重置访问密码。')
    return output


def main():
    p = argparse.ArgumentParser(description='下载和构建游戏、美食词库')
    p.add_argument('--download', action='store_true')
    p.add_argument('--refresh-sources', action='store_true', help='接受上游数据更新并更新校验值')
    args = p.parse_args()
    if args.refresh_sources and not args.download:
        p.error('--refresh-sources 需要 --download')
    try:
        build_wordlist(download=args.download, refresh_sources=args.refresh_sources)
    except ValueError as exc:
        p.error(str(exc))

if __name__ == '__main__':
    main()
