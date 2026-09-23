import os
import base64
import ssl
import socket
import time
import json
import urllib3.util.connection
import urllib3
from dotenv import load_dotenv

load_dotenv()

urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET


def _create_session():
    context = ssl.create_default_context()
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    return urllib3.PoolManager(ssl_context=context)

def recognize_receipt(image_bytes: bytes) -> str:
    url = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {os.getenv('YANDEX_API_KEY')}",
        "x-folder-id": os.getenv("YANDEX_FOLDER_ID"),
    }
    body = {
        "mimeType": "image/jpeg",
        "languageCodes": ["*"],
        "model": "page",
        "content": base64.b64encode(image_bytes).decode("utf-8"),
    }
    response = None
    for attempt in range(3):
        try:
            with _create_session() as session:
                response = session.request(
                    "POST",
                    url,
                    headers=headers,
                    body=json.dumps(body).encode("utf-8"),
                    timeout=urllib3.Timeout(connect=30, read=180),
                )
        except urllib3.exceptions.HTTPError:
            if attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))
            continue

        if response.status not in (429,) and response.status < 500:
            break
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))

    # === ДИАГНОСТИКА: печатаем полный ответ при ошибке ===
    if response.status != 200:
        print("=" * 60)
        print("YANDEX OCR ERROR")
        print("Status code:", response.status)
        print("Response headers:", dict(response.headers))
        print("Response body:")
        print(response.data.decode("utf-8", errors="replace"))
        print("=" * 60)

    if response.status >= 400:
        raise RuntimeError(f"Yandex OCR HTTP {response.status}: {response.data[:500]!r}")

    data = json.loads(response.data)
    blocks = data.get("result", {}).get("textAnnotation", {}).get("blocks", [])
    lines = []
    for block in blocks:
        for line in block.get("lines", []):
            text = "".join(w.get("text", "") for w in line.get("words", []))
            lines.append(text)
    return "\n".join(lines)