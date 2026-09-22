import os
import json
import requests
from dotenv import load_dotenv
from prompts import PARSE_PROMPT, RECIPE_PROMPT

load_dotenv()

FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
API_KEY = os.getenv("YANDEX_API_KEY")
MODEL_URI = f"gpt://{FOLDER_ID}/yandexgpt-lite/latest"
API_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

def _call_yandexgpt(system, user, temperature=0.2, max_tokens=2000):
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
    r = requests.post(API_URL, headers=headers, json=body)
    r.raise_for_status()
    return r.json()["result"]["alternatives"][0]["message"]["text"]

def _extract_json(text):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"В ответе нет JSON: {text[:200]}")
    return json.loads(text[start:end + 1])

def parse_receipt_text(raw_text):
    system_prompt = PARSE_PROMPT + "\n\nВАЖНО: Верни ответ строго в формате JSON без пояснений."
    result = _call_yandexgpt(system=system_prompt, user=raw_text, temperature=0.1)
    data = _extract_json(result)
    products = data.get("products", [])
    return _deduplicate(products)

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

def generate_recipes(products):
    product_list = "\n".join(f"- {p['name']} ({p['quantity']} {p['unit']})" for p in products)
    return _call_yandexgpt(
        system=RECIPE_PROMPT,
        user=f"Продукты, которые скоро испортятся:\n{product_list}",
        temperature=0.4, max_tokens=1500,
    )