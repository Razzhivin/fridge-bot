import asyncio
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from db import (
    add_product,
    can_process_receipt,
    consume_photo_quota,
    delete_all,
    delete_expired,
    delete_product,
    get_all_for_cooking,
    get_fridge,
    init_db,
)
from llm import generate_recipes, parse_receipt_text
from ocr import recognize_receipt


MAX_PHOTO_BYTES = int(os.getenv("MAX_PHOTO_BYTES", str(10 * 1024 * 1024)))


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    quantity: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=20)
    price: float = Field(ge=0)
    category: str = Field(min_length=1, max_length=40)
    purchase_date: str | None = None


class ProductsCreate(BaseModel):
    products: list[ProductCreate] = Field(min_length=1, max_length=100)
    purchase_date: str | None = None


def _normalize_phone(phone: str) -> str:
    value = re.sub(r"[\s()\-]", "", phone)
    if value.startswith("8") and len(value) == 11:
        value = "+7" + value[1:]
    if not re.fullmatch(r"\+[1-9]\d{9,14}", value):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Укажите номер телефона в международном формате",
        )
    return value


def current_user(x_phone_number: str | None = Header(default=None)) -> str:
    if not x_phone_number:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется заголовок X-Phone-Number",
        )
    return _normalize_phone(x_phone_number)


def _product_response(row):
    product_id, name, quantity, unit, price, category, expiry, purchase_date, status_value, created_at = row
    return {
        "id": product_id,
        "name": name,
        "quantity": quantity,
        "unit": unit,
        "price": price,
        "category": category,
        "expiry_date": expiry,
        "purchase_date": purchase_date,
        "status": status_value,
        "created_at": created_at,
    }


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="FridgeBot API", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/v1/products")
async def list_products(user_id: str = Depends(current_user)):
    return {"products": [_product_response(row) for row in get_fridge(user_id)]}


@app.post("/api/v1/products", status_code=status.HTTP_201_CREATED)
async def create_product(product: ProductCreate, user_id: str = Depends(current_user)):
    product_id = add_product(
        user_id,
        product.name,
        product.quantity,
        product.unit,
        product.price,
        product.category,
        product.purchase_date,
    )
    return {"id": product_id}


@app.post("/api/v1/products/bulk", status_code=status.HTTP_201_CREATED)
async def create_products(payload: ProductsCreate, user_id: str = Depends(current_user)):
    product_ids = [
        add_product(
            user_id,
            product.name,
            product.quantity,
            product.unit,
            product.price,
            product.category,
            product.purchase_date or payload.purchase_date,
        )
        for product in payload.products
    ]
    return {"ids": product_ids}


@app.delete("/api/v1/products/expired")
async def remove_expired(user_id: str = Depends(current_user)):
    return {"deleted": delete_expired(user_id)}


@app.delete("/api/v1/products")
async def remove_all(user_id: str = Depends(current_user)):
    return {"deleted": delete_all(user_id)}


@app.delete("/api/v1/products/{product_id}")
async def remove_product(product_id: int, user_id: str = Depends(current_user)):
    if not delete_product(user_id, product_id):
        raise HTTPException(status_code=404, detail="Товар не найден")
    return {"deleted": True}


@app.post("/api/v1/receipts/parse")
async def parse_receipt(
    receipt: UploadFile = File(...),
    user_id: str = Depends(current_user),
):
    if not can_process_receipt(user_id):
        raise HTTPException(status_code=429, detail="Лимит чеков исчерпан")
    image_bytes = await receipt.read(MAX_PHOTO_BYTES + 1)
    if len(image_bytes) > MAX_PHOTO_BYTES:
        raise HTTPException(status_code=413, detail="Фото слишком большое")
    raw_text = await asyncio.to_thread(recognize_receipt, image_bytes)
    products, purchase_date = await asyncio.to_thread(parse_receipt_text, raw_text)
    consume_photo_quota(user_id)
    return {"purchase_date": purchase_date, "products": products}


@app.post("/api/v1/recipes")
async def recipes(user_id: str = Depends(current_user)):
    products = get_all_for_cooking(user_id)
    if not products:
        raise HTTPException(status_code=404, detail="В холодильнике нет продуктов для готовки")
    lines = []
    for name, quantity, unit, _category, expiry in products:
        marker = "без срока"
        if expiry:
            days_left = (datetime.strptime(expiry, "%Y-%m-%d") - datetime.now()).days
            marker = "СРОЧНО" if days_left <= 3 else "скоро" if days_left <= 7 else "свежее"
        lines.append(f"{marker} | {name} — {quantity} {unit}")
    result = await asyncio.to_thread(generate_recipes, "\n".join(lines))
    return {"recipes": result}