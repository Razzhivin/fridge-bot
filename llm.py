import os
import json
import ssl
import socket
import requests
import urllib3.util.connection
from dotenv import load_dotenv
import urllib3
from prompts import PARSE_PROMPT, RECIPE_PROMPT
load_dotenv()

urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET


def _create_session():
    context = ssl.create_default_context()
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    return urllib3.PoolManager(ssl_context=context)

FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
API_KEY = os.getenv("YANDEX_API_KEY")
MODEL_URI = f"gpt://{FOLDER_ID}/yandexgpt/latest"
API_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

def _call_yandexgpt(system, user, temperature=0.2, max_tokens=2000, timeout=120):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {API_KEY}",
        "x-folder-id": FOLDER_ID,
    }
    body = {
        "modelUri": MODEL_URI,
        "completionOptions": {"stream": False, "temperature": temperature, "maxTokens": str(max_tokens)},
        "messages": [
            {"role": "system", "text": system},
            {"role": "user", "text": user},
        ],
    }
    with _create_session() as session:
        response = session.request(
            "POST",
            API_URL,
            headers=headers,
            body=json.dumps(body).encode("utf-8"),
            timeout=urllib3.Timeout(connect=30, read=timeout),
        )
    if response.status >= 400:
        raise RuntimeError(f"YandexGPT HTTP {response.status}: {response.data[:500]!r}")
    return json.loads(response.data)["result"]["alternatives"][0]["message"]["text"]

def _extract_json(text):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"В ответе нет JSON: {text[:200]}")
    return json.loads(text[start:end + 1])

def parse_receipt_text(raw_text):
    system_prompt = PARSE_PROMPT
    result = _call_yandexgpt(system=system_prompt, user=raw_text, temperature=0.1)
    data = _extract_json(result)
    products = data.get("products", [])
    purchase_date = data.get("purchase_date")
    products = _deduplicate(products)
    return products, purchase_date

def _deduplicate(products):
    """Объединяет товары с одинаковыми названиями."""
    merged = {}
    for p in products:
        key = (p["name"].strip().lower(), p.get("unit", "шт"))
        if key in merged:
            merged[key]["quantity"] += p["quantity"]
            merged[key]["price"] += p["price"]
        else:
            merged[key] = p.copy()
    return list(merged.values())

def generate_recipes(product_list: str) -> str:
    return _call_yandexgpt(
        system=RECIPE_PROMPT,
        user=(
            "Продукты в холодильнике. Используй продукты с 🔴 в первую очередь, "
            "затем 🟡, 🟢 и ⚪:\n\n" + product_list
        ),
        temperature=0.7,
        max_tokens=2000,
        timeout=180,
    )