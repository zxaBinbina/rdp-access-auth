"""Optional online verification; downloads public deployment assets into .build only."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy import download_connector
from tools.build_wordlist import build_wordlist

if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1] / '.build/deploy-downloads'
    root.mkdir(parents=True, exist_ok=True)
    build_wordlist(root, download=True)
    download_connector(root / 'cloudflared', print)
    print('部署下载与校验通过；未写入系统部署目录。')
