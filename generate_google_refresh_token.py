"""
Запусти локально на Mac:
    pip3 install requests
    python3 generate_google_refresh_token.py

Что нужно заранее:
1. console.cloud.google.com → твой проект
2. APIs & Services → Library → включи "Google Drive API"
3. APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID
   - Application type: Desktop App
4. Скопируй Client ID и Client Secret → вставь ниже
5. OAuth consent screen → Test Users → добавь kotovandrii00@gmail.com
"""

CLIENT_ID = "ВСТАВЬ_CLIENT_ID"
CLIENT_SECRET = "ВСТАВЬ_CLIENT_SECRET"

# ---------- не меняй ниже ----------

import json
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

REDIRECT_URI = "http://localhost:8080"
SCOPE = "https://www.googleapis.com/auth/drive.file"

auth_url = (
    "https://accounts.google.com/o/oauth2/v2/auth?"
    + urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    })
)

print("Открываю браузер...")
webbrowser.open(auth_url)
print("Если браузер не открылся, перейди вручную:")
print(auth_url)
print()

auth_code = None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        params = parse_qs(urlparse(self.path).query)
        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h1>OK! Можно закрыть это окно и вернуться в терминал.</h1>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<h1>Error: no code received</h1>")

    def log_message(self, format, *args):
        pass


print("Жду авторизацию на http://localhost:8080 ...")
HTTPServer(("localhost", 8080), Handler).handle_request()

if not auth_code:
    print("Ошибка: код авторизации не получен.")
    exit(1)

print("Код получен, получаю токены...")

resp = requests.post(
    "https://oauth2.googleapis.com/token",
    data={
        "code": auth_code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    },
)
tokens = resp.json()

if "refresh_token" not in tokens:
    print("Ошибка:")
    print(json.dumps(tokens, indent=2))
    exit(1)

print()
print("=" * 60)
print("Вставь в Railway Variables:")
print("=" * 60)
print(f"GOOGLE_CLIENT_ID={CLIENT_ID}")
print(f"GOOGLE_CLIENT_SECRET={CLIENT_SECRET}")
print(f"GOOGLE_REFRESH_TOKEN={tokens['refresh_token']}")
print("=" * 60)
