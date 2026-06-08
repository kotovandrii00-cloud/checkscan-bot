"""
Запусти этот скрипт локально на Mac чтобы получить GOOGLE_OAUTH_REFRESH_TOKEN.

Что нужно сделать заранее:
1. Открой https://console.cloud.google.com/
2. Выбери проект (или создай новый)
3. APIs & Services → Enable APIs → включи "Google Drive API"
4. APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID
5. Application type: Desktop App
6. Скопируй Client ID и Client Secret
7. Вставь их ниже в CLIENT_ID и CLIENT_SECRET

После запуска скрипта:
- откроется браузер с запросом доступа к Google Drive
- после подтверждения в терминале появится refresh_token
- вставь его в Railway переменную GOOGLE_OAUTH_REFRESH_TOKEN
"""

CLIENT_ID = "ВСТАВЬ_CLIENT_ID_ЗДЕСЬ"
CLIENT_SECRET = "ВСТАВЬ_CLIENT_SECRET_ЗДЕСЬ"

# ------- не меняй ниже -------

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

print("Открываю браузер для авторизации...")
print("URL:", auth_url)
webbrowser.open(auth_url)

auth_code = None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        params = parse_qs(urlparse(self.path).query)
        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h1>OK! Можно закрыть это окно.</h1>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<h1>Error: no code</h1>")

    def log_message(self, format, *args):
        pass


print("Жду авторизацию на http://localhost:8080 ...")
server = HTTPServer(("localhost", 8080), Handler)
server.handle_request()

if not auth_code:
    print("Ошибка: код авторизации не получен.")
    exit(1)

print("Код получен, обмениваю на токены...")

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
    print("Ошибка получения токена:")
    print(json.dumps(tokens, indent=2))
    exit(1)

print()
print("=" * 60)
print("ГОТОВО! Вставь в Railway переменные:")
print("=" * 60)
print(f"GOOGLE_OAUTH_CLIENT_ID={CLIENT_ID}")
print(f"GOOGLE_OAUTH_CLIENT_SECRET={CLIENT_SECRET}")
print(f"GOOGLE_OAUTH_REFRESH_TOKEN={tokens['refresh_token']}")
print("=" * 60)
