"""Local visual preview with sample data; never grants real access."""
from pathlib import Path
import secrets

from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)
template = Path(__file__).resolve().parents[1] / 'portal.html'


@app.route('/', methods=['GET'])
@app.route('/authorize', methods=['POST'])
@app.route('/credentials', methods=['GET'])
def preview():
    method = request.values.get('method', 'password')
    if method not in ('password', 'temporary', 'passkey'):
        method = 'password'
    nonce = secrets.token_urlsafe(18)
    body = render_template_string(
        template.read_text(), nonce=nonce, method=method,
        manage=request.path == '/credentials', success=False,
        message='本地样式预览，不执行访问授权。' if request.method == 'POST' else '',
        csrf='preview-only', ipv6=False, client_ip='203.0.113.10',
        turnstile_site_key='', keys=[], current_temporary='示例词一-示例词二-示例词三',
    )
    return body, 200, {
        'Cache-Control': 'no-store',
        'Content-Security-Policy': (
            f"default-src 'none'; img-src data:; style-src 'nonce-{nonce}'; "
            f"script-src 'nonce-{nonce}'; connect-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        ),
    }


@app.post('/passkeys/<path:operation>')
def preview_passkey(operation):
    return jsonify(error='本地样式预览，不执行通行密钥操作。'), 400


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=18123, debug=False)
