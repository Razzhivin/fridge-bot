import os
import base64
import requests
from dotenv import load_dotenv

load_dotenv()

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
    response = requests.post(url, headers=headers, json=body)

    # === ДИАГНОСТИКА: печатаем полный ответ при ошибке ===
    if response.status_code != 200:
        print("=" * 60)
        print("YANDEX OCR ERROR")
        print("Status code:", response.status_code)
        print("Response headers:", dict(response.headers))
        print("Response body:")
        print(response.text)
        print("=" * 60)

    response.raise_for_status()

    data = response.json()
    blocks = data.get("result", {}).get("textAnnotation", {}).get("blocks", [])
    lines = []
    for block in blocks:
        for line in block.get("lines", []):
            text = "".join(w.get("text", "") for w in line.get("words", []))
            lines.append(text)
    return "\n".join(lines)