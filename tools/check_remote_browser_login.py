"""Real HTTPS/browser regression check, with local-only fake credentials."""
import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.async_api import async_playwright
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from room_service.remote import RemoteSettings, create_remote_app

PASSWORD = 'local-browser-test-' + 'b' * 48


async def main():
    scratch = ROOT / 'tmp'
    scratch.mkdir(exist_ok=True)
    browser_path = os.environ.get('ROOM_TEST_BROWSER') or next((str(path) for path in [
        Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'),
        Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
    ] if path.is_file()), None) or shutil.which('google-chrome') or shutil.which('chromium')
    with tempfile.TemporaryDirectory(dir=scratch) as folder:
        folder = Path(folder)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(key, hashes.SHA256()))
        (folder / 'cert.pem').write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        (folder / 'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        origin = f'https://localhost:{port}'
        app = create_remote_app(RemoteSettings('local-test-mcp-' + 'a' * 48, PASSWORD, 'https://localhost', folder / 'room.db'))
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, access_log=False, log_level='error', timeout_graceful_shutdown=2,
            ssl_certfile=str(folder / 'cert.pem'), ssl_keyfile=str(folder / 'key.pem')))
        thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[listener]), loop_factory=asyncio.SelectorEventLoop), daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.started:
                    break
                await asyncio.sleep(0.05)
            assert server.started
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(executable_path=browser_path, headless=True)
                try:
                    page = await browser.new_page(ignore_https_errors=True)
                    origins, errors = [], []
                    page.on('pageerror', lambda error: errors.append(str(error)))

                    async def capture(request):
                        if request.method == 'POST' and request.url == origin + '/login':
                            origins.append((await request.all_headers()).get('origin'))

                    page.on('request', capture)
                    await page.goto(origin + '/login')
                    await page.fill('#password', 'incorrect-local-test-password')
                    await page.click('button')
                    await page.wait_for_function("document.querySelector('#login-error').textContent === '密码不正确。'")
                    await page.fill('#password', PASSWORD)
                    await page.click('button')
                    await page.wait_for_url(origin + '/', wait_until='domcontentloaded')
                    assert origins == [origin, origin], origins
                    result = await page.evaluate("fetch('/api/v1/room/state').then(async response => ({status:response.status, ids:(await response.json()).characters.map(item=>item.id)}))")
                    assert result == {'status': 200, 'ids': ['owner', 'companion']}, result
                    await page.wait_for_function("document.querySelector('#room-game canvas') !== null")
                    await page.wait_for_function("document.querySelector('#room-loading').classList.contains('is-ready')")
                    assert not errors, errors
                    await page.screenshot(path=str(scratch / 'remote-browser.png'))
                    print('BROWSER_REAL_HTTPS_LOGIN_COOKIE_STATE_CANVAS_OK', json.dumps({'origin': origin, 'status': result['status'], 'page_errors': len(errors)}))
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()
            assert not thread.is_alive(), 'local test server did not stop'


asyncio.run(main())
