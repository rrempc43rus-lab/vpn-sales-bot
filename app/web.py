from __future__ import annotations

import base64
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from aiogram import Bot
from fastapi import APIRouter, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings
from .db import Database, order_links, plan_inbound_ids
from .interface import asset_web_path, get_interface_settings, project_root, save_uploaded_asset, support_contact_url
from .legal import PRIVACY_POLICY, USER_AGREEMENT
from .platega import PlategaClient
from .services import handle_successful_order_payment, notify_admin, send_delivery_message
from .xui import XuiClient, normalize_share_link, normalize_subscription_payload


TEXT_SETTING_FIELDS = [
    "payment_instructions",
    "support_contact",
    "brand_name",
    "brand_tagline",
    "welcome_text",
    "features_text",
    "about_text",
    "setup_text",
    "plans_intro",
    "support_text",
    "delivery_text",
    "button_buy",
    "button_plans",
    "button_orders",
    "button_profile",
    "button_about",
    "button_setup",
    "button_support",
    "button_open_subscription",
    "button_short_link",
    "button_write_support",
    "button_privacy_policy",
    "button_user_agreement",
    "referral_reward_percent",
    "referral_new_user_discount_percent",
    "loyalty_discount_percent",
    "loyalty_orders_threshold",
    "max_bonus_writeoff_percent",
]


def status_label(status: str) -> str:
    return {
        "pending_payment": "Ожидает оплату",
        "waiting_review": "Ожидает проверку",
        "paid": "Оплачен",
        "delivered": "Выдан",
        "delivery_failed": "Ошибка выдачи",
        "cancelled": "Отменен",
    }.get(status, status)


def require_admin(request: Request, settings: Settings) -> RedirectResponse | None:
    if request.session.get("admin") != settings.admin_username:
        return RedirectResponse("/admin/login", status_code=302)
    return None


def row_to_plan_form(plan: sqlite3.Row | None) -> dict[str, object]:
    if plan is None:
        return {
            "name": "",
            "price_rub": 0,
            "duration_days": 30,
            "traffic_gb": 0,
            "description": "",
            "inbound_ids": [],
            "is_active": True,
        }
    return {
        "name": plan["name"],
        "price_rub": int(plan["price_rub"]),
        "duration_days": int(plan["duration_days"]),
        "traffic_gb": int(plan["traffic_gb"]),
        "description": plan["description"],
        "inbound_ids": plan_inbound_ids(plan),
        "is_active": bool(plan["is_active"]),
    }


def legal_page_url(slug: str) -> str:
    return f"/{slug}"


def build_legal_nav() -> list[dict[str, str]]:
    return [
        {"title": "Политика конфиденциальности", "url": legal_page_url(PRIVACY_POLICY["slug"])},
        {"title": "Пользовательское соглашение", "url": legal_page_url(USER_AGREEMENT["slug"])},
        {"title": "Контакты поддержки", "url": "/support"},
    ]


def build_subscription_headers(settings: Settings, db: Database) -> dict[str, str]:
    ui_values = get_interface_settings(db, settings)
    brand_name = ui_values.get("brand_name", settings.bot_name).strip() or settings.bot_name
    brand_tagline = ui_values.get("brand_tagline", "").strip()
    title = brand_name
    if brand_tagline:
        title = f"{title} | {brand_tagline}"
    encoded_title = base64.b64encode(title.encode("utf-8")).decode("ascii")
    headers = {
        "profile-title": f"base64:{encoded_title}",
        "profile-update-interval": "12",
        "content-disposition": 'attachment; filename="subscription.txt"',
        "profile-web-page-url": f"{settings.public_base_url.rstrip('/')}/support",
    }
    support_url = support_contact_url(ui_values.get("support_contact", settings.support_contact))
    if support_url:
        headers["support-url"] = support_url
    return headers


def build_router(
    *,
    settings: Settings,
    db: Database,
    bot: Bot,
    xui: XuiClient,
    platega: PlategaClient,
) -> APIRouter:
    router = APIRouter()
    base_dir = Path(__file__).resolve().parent.parent
    templates = Jinja2Templates(directory=str(base_dir / "templates"))
    payment_callback_url = f"{settings.public_base_url.rstrip('/')}/api/payment-callback"

    async def process_platega_event(
        order: sqlite3.Row,
        *,
        provider_status: str,
        transaction_id: str,
        payment_method: str,
        raw_payload: dict[str, object],
        source_label: str,
    ) -> None:
        db.update_order_payment(
            int(order["id"]),
            provider="platega",
            transaction_id=transaction_id,
            provider_status=provider_status,
            payment_method=payment_method or str(order["payment_method"] or ""),
            raw_payload=raw_payload,
        )

        if provider_status == "CONFIRMED":
            await handle_successful_order_payment(
                bot,
                settings,
                db,
                xui,
                int(order["id"]),
                payment_note="Platega payment confirmed",
                admin_message=f"✅ Оплата подтверждена: {order['telegram_id']}",
            )
            return

        if provider_status in {"CANCELED", "CHARGEBACK"}:
            if str(order["status"]) != "delivered":
                db.update_order_status(
                    int(order["id"]),
                    "cancelled",
                    payment_note=f"Platega status: {provider_status}",
                )

    @router.get("/", include_in_schema=False)
    async def home() -> RedirectResponse:
        return RedirectResponse("/admin", status_code=302)

    @router.get("/legal", response_class=HTMLResponse)
    async def legal_index(request: Request):
        ui_values = get_interface_settings(db, settings)
        support_contact = ui_values.get("support_contact", settings.support_contact)
        return templates.TemplateResponse(
            "legal_index.html",
            {
                "request": request,
                "bot_name": ui_values.get("brand_name", settings.bot_name),
                "brand_tagline": ui_values.get("brand_tagline", ""),
                "support_contact": support_contact,
                "support_contact_url": support_contact_url(support_contact),
                "nav_items": build_legal_nav(),
            },
        )

    @router.get("/privacy-policy", response_class=HTMLResponse)
    async def privacy_policy_page(request: Request):
        ui_values = get_interface_settings(db, settings)
        return templates.TemplateResponse(
            "legal_page.html",
            {
                "request": request,
                "bot_name": ui_values.get("brand_name", settings.bot_name),
                "brand_tagline": ui_values.get("brand_tagline", ""),
                "page": PRIVACY_POLICY,
                "nav_items": build_legal_nav(),
            },
        )

    @router.get("/user-agreement", response_class=HTMLResponse)
    async def user_agreement_page(request: Request):
        ui_values = get_interface_settings(db, settings)
        return templates.TemplateResponse(
            "legal_page.html",
            {
                "request": request,
                "bot_name": ui_values.get("brand_name", settings.bot_name),
                "brand_tagline": ui_values.get("brand_tagline", ""),
                "page": USER_AGREEMENT,
                "nav_items": build_legal_nav(),
            },
        )

    @router.get("/support", response_class=HTMLResponse)
    async def support_page(request: Request):
        ui_values = get_interface_settings(db, settings)
        support_contact = ui_values.get("support_contact", settings.support_contact)
        return templates.TemplateResponse(
            "support_page.html",
            {
                "request": request,
                "bot_name": ui_values.get("brand_name", settings.bot_name),
                "brand_tagline": ui_values.get("brand_tagline", ""),
                "support_text": ui_values.get("support_text", ""),
                "support_contact": support_contact,
                "support_contact_url": support_contact_url(support_contact),
                "nav_items": build_legal_nav(),
            },
        )

    @router.post("/api/payment-callback")
    async def payment_callback(request: Request):
        if not settings.platega_enabled:
            raise HTTPException(status_code=503, detail="Payment provider is disabled")
        if not platega.verify_callback_headers(request.headers):
            raise HTTPException(status_code=401, detail="Invalid callback headers")

        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid callback payload")

        callback = platega.parse_callback(payload)
        if not callback.transaction_id or not callback.status:
            raise HTTPException(status_code=400, detail="Incomplete callback payload")

        order = None
        if callback.order_ref:
            order = db.get_order_by_public_id(callback.order_ref)
        if order is None:
            order = db.get_order_by_payment_transaction_id(callback.transaction_id)
        if order is None:
            return {"ok": True, "ignored": "order_not_found"}

        await process_platega_event(
            order,
            provider_status=callback.status,
            transaction_id=callback.transaction_id,
            payment_method=callback.payment_method,
            raw_payload=callback.raw_payload,
            source_label="Webhook",
        )
        return {"ok": True, "status": callback.status}

    @router.get("/s/{public_id}")
    async def short_subscription(public_id: str):
        order = db.get_order_by_public_id(public_id)
        if order is None or not order["xui_subscription_url"]:
            raise HTTPException(status_code=404, detail="Subscription not found")
        return RedirectResponse(f"/sub/{public_id}", status_code=302)

    @router.get("/sub/{public_id}")
    async def proxy_subscription(public_id: str, request: Request):
        order = db.get_order_by_public_id(public_id)
        if order is None or not order["xui_subscription_url"]:
            raise HTTPException(status_code=404, detail="Subscription not found")
        target = str(order["xui_subscription_url"])
        response_headers = build_subscription_headers(settings, db)
        scheme = urlsplit(target).scheme.lower()
        if scheme in {"vless", "vmess", "trojan", "ss", "ssr"}:
            direct_link = normalize_share_link(settings.public_base_url, target)
            payload = base64.b64encode(f"{direct_link}\n".encode("utf-8"))
            return Response(content=payload, media_type="text/plain; charset=utf-8", headers=response_headers)
        async with xui._lock:
            upstream = await xui._client.get(target, headers={"User-Agent": request.headers.get("User-Agent", "")})
        upstream.raise_for_status()
        content_type = upstream.headers.get("Content-Type", "text/plain; charset=utf-8")
        content = normalize_subscription_payload(settings.public_base_url, upstream.content)
        passthrough_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() in {"subscription-userinfo", "profile-update-interval", "support-url", "profile-web-page-url"}
        }
        passthrough_headers.update(response_headers)
        return Response(content=content, media_type=content_type, headers=passthrough_headers)

    @router.get("/admin/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse("login.html", {"request": request, "error": "", "bot_name": settings.bot_name})

    @router.post("/admin/login", response_class=HTMLResponse)
    async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
        if username == settings.admin_username and password == settings.admin_password:
            request.session["admin"] = settings.admin_username
            return RedirectResponse("/admin", status_code=302)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Неверный логин или пароль", "bot_name": settings.bot_name},
            status_code=400,
        )

    @router.get("/admin/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/admin/login", status_code=302)

    @router.get("/admin", response_class=HTMLResponse)
    async def dashboard(request: Request):
        redirect = require_admin(request, settings)
        if redirect:
            return redirect
        live_inbounds = await xui.list_inbounds()
        users = db.list_users()
        stats = {
            "plans": len(db.list_plans()),
            "orders": len(db.list_orders()),
            "users": len(users),
            "inbounds": len(live_inbounds),
            "referrals": sum(int(user["referred_users_count"] or 0) for user in users),
            "bonus_pool_rub": sum(int(user["referral_bonus_balance_rub"] or 0) for user in users),
        }
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "stats": stats,
                "orders": db.list_orders()[:8],
                "plans": db.list_plans(),
                "inbounds": live_inbounds,
                "settings": db.get_settings(),
                "loyalty": db.get_loyalty_settings(),
                "bot_name": settings.bot_name,
                "status_label": status_label,
                "payment_callback_url": payment_callback_url,
                "platega_enabled": settings.platega_enabled,
            },
        )

    @router.get("/admin/plans", response_class=HTMLResponse)
    async def plans_page(request: Request):
        redirect = require_admin(request, settings)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            "plans.html",
            {
                "request": request,
                "plans": db.list_plans(),
                "inbounds": await xui.list_inbounds(),
                "plan_form": row_to_plan_form(None),
                "editing_id": None,
                "bot_name": settings.bot_name,
            },
        )

    @router.get("/admin/plans/{plan_id}", response_class=HTMLResponse)
    async def edit_plan_page(request: Request, plan_id: int):
        redirect = require_admin(request, settings)
        if redirect:
            return redirect
        plan = db.get_plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="Plan not found")
        return templates.TemplateResponse(
            "plans.html",
            {
                "request": request,
                "plans": db.list_plans(),
                "inbounds": await xui.list_inbounds(),
                "plan_form": row_to_plan_form(plan),
                "editing_id": plan_id,
                "bot_name": settings.bot_name,
            },
        )

    @router.post("/admin/plans/save")
    async def save_plan(
        request: Request,
        plan_id: int | None = Form(default=None),
        name: str = Form(...),
        price_rub: int = Form(...),
        duration_days: int = Form(...),
        traffic_gb: int = Form(...),
        description: str = Form(""),
        inbound_ids: list[int] = Form(default=[]),
        is_active: str | None = Form(default=None),
    ):
        redirect = require_admin(request, settings)
        if redirect:
            return redirect
        db.save_plan(
            plan_id=plan_id,
            name=name.strip(),
            price_rub=price_rub,
            duration_days=duration_days,
            traffic_gb=traffic_gb,
            description=description.strip(),
            inbound_ids=[int(item) for item in inbound_ids],
            is_active=is_active == "on",
        )
        return RedirectResponse("/admin/plans", status_code=302)

    @router.post("/admin/plans/{plan_id}/delete")
    async def delete_plan(request: Request, plan_id: int):
        redirect = require_admin(request, settings)
        if redirect:
            return redirect
        db.delete_plan(plan_id)
        return RedirectResponse("/admin/plans", status_code=302)

    @router.get("/admin/orders", response_class=HTMLResponse)
    async def orders_page(request: Request):
        redirect = require_admin(request, settings)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            "orders.html",
            {
                "request": request,
                "orders": db.list_orders(),
                "bot_name": settings.bot_name,
                "order_links": order_links,
                "status_label": status_label,
            },
        )

    @router.post("/admin/orders/{order_id}/status")
    async def set_order_status(request: Request, order_id: int, status: str = Form(...)):
        redirect = require_admin(request, settings)
        if redirect:
            return redirect
        db.update_order_status(order_id, status)
        return RedirectResponse("/admin/orders", status_code=302)

    @router.post("/admin/orders/{order_id}/deliver")
    async def deliver(request: Request, order_id: int):
        redirect = require_admin(request, settings)
        if redirect:
            return redirect
        order = db.get_order(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found")
        try:
            await handle_successful_order_payment(
                bot,
                settings,
                db,
                xui,
                order_id,
                payment_note=str(order["payment_note"] or "Manual delivery"),
            )
        except Exception as exc:  # noqa: BLE001
            await notify_admin(
                bot,
                settings.admin_telegram_id,
                f"Ручная выдача заказа #{order['public_id']} завершилась ошибкой: {exc}",
            )
        return RedirectResponse("/admin/orders", status_code=302)

    @router.post("/admin/orders/{order_id}/sync-payment")
    async def sync_payment(request: Request, order_id: int):
        redirect = require_admin(request, settings)
        if redirect:
            return redirect
        order = db.get_order(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found")
        if str(order["payment_provider"]) != "platega" or not order["payment_transaction_id"]:
            return RedirectResponse("/admin/orders", status_code=302)
        try:
            payload = await platega.get_transaction(str(order["payment_transaction_id"]))
        except Exception as exc:  # noqa: BLE001
            await notify_admin(
                bot,
                settings.admin_telegram_id,
                f"Не удалось синхронизировать платеж заказа #{order['public_id']}: {exc}",
            )
            return RedirectResponse("/admin/orders", status_code=302)

        await process_platega_event(
            order,
            provider_status=str(payload.get("status") or order["payment_status"] or "").upper(),
            transaction_id=str(order["payment_transaction_id"]),
            payment_method=str(payload.get("paymentMethod") or order["payment_method"] or ""),
            raw_payload=payload,
            source_label="Admin sync",
        )
        return RedirectResponse("/admin/orders", status_code=302)

    @router.post("/admin/orders/{order_id}/resend")
    async def resend_delivery(request: Request, order_id: int):
        redirect = require_admin(request, settings)
        if redirect:
            return redirect
        order = db.get_order(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found")
        if not order["xui_subscription_url"]:
            return RedirectResponse("/admin/orders", status_code=302)
        await send_delivery_message(bot, settings, db, int(order["telegram_id"]), order)
        await notify_admin(
            bot,
            settings.admin_telegram_id,
            f"Заказ #{order['public_id']} повторно отправлен пользователю {order['telegram_id']}.",
        )
        return RedirectResponse("/admin/orders", status_code=302)

    @router.get("/admin/users", response_class=HTMLResponse)
    async def users_page(request: Request):
        redirect = require_admin(request, settings)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            "users.html",
            {"request": request, "users": db.list_users(), "bot_name": settings.bot_name},
        )

    @router.get("/admin/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        redirect = require_admin(request, settings)
        if redirect:
            return redirect
        ui_values = get_interface_settings(db, settings)
        return templates.TemplateResponse(
            "settings.html",
            {
                "request": request,
                "ui_values": ui_values,
                "hero_image_url": asset_web_path(ui_values.get("hero_image_path")),
                "delivery_image_url": asset_web_path(ui_values.get("delivery_image_path")),
                "inbounds": await xui.list_inbounds(),
                "bot_name": settings.bot_name,
            },
        )

    @router.post("/admin/settings")
    async def save_settings(request: Request):
        redirect = require_admin(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        for key in TEXT_SETTING_FIELDS:
            value = str(form.get(key, "")).strip()
            db.set_setting(key, value)

        current_ui = get_interface_settings(db, settings)

        hero_image = form.get("hero_image")
        if isinstance(hero_image, UploadFile) and hero_image.filename:
            hero_path = await save_uploaded_asset(hero_image, "hero")
            db.set_setting("hero_image_path", hero_path)
        elif form.get("clear_hero_image") == "on":
            db.set_setting("hero_image_path", "static/bot-hero.png")
        else:
            db.set_setting("hero_image_path", current_ui.get("hero_image_path", "static/bot-hero.png"))

        delivery_image = form.get("delivery_image")
        if isinstance(delivery_image, UploadFile) and delivery_image.filename:
            delivery_path = await save_uploaded_asset(delivery_image, "delivery")
            db.set_setting("delivery_image_path", delivery_path)
        elif form.get("clear_delivery_image") == "on":
            db.set_setting("delivery_image_path", "")

        return RedirectResponse("/admin/settings", status_code=302)

    return router


def mount_static(app: FastAPI) -> None:
    base_dir = project_root()
    media_dir = base_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(base_dir / "static")), name="static")
    app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")
