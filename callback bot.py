"""
Бот для заявок на обратный звонок.

Запуск:  python callback_bot.py
Потом открой в браузере: http://localhost:5000
"""
import csv
import json
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


def bootstrap():
    """Один раз сам доустанавливает flask, playwright и браузер Chromium."""
    marker = Path(__file__).parent / ".installed"
    try:
        import flask  # noqa: F401
        import playwright  # noqa: F401
    except ImportError:
        print(">>> Устанавливаю flask и playwright...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "flask", "playwright"])
    if not marker.exists():
        print(">>> Устанавливаю браузер (2-3 минуты, подожди)...")
        r = subprocess.call([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"])
        if r != 0:  # например, нет sudo: ставим без системных библиотек
            subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        marker.write_text("ok")
        print(">>> Готово.")


bootstrap()

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = Path(__file__).parent
SHOTS = BASE / "screenshots"
SHOTS.mkdir(exist_ok=True)
DONE_FILE = BASE / "done.json"
LOG_FILE = BASE / "log.csv"

# Пауза между сайтами (секунды), чтобы не выглядеть как спам
DELAY = (15, 40)

PHONE_SEL = [
    'input[type="tel"]',
    'input[name*="phone" i]',
    'input[name*="tel" i]',
    'input[id*="phone" i]',
    'input[placeholder*="телефон" i]',
    'input[placeholder*="номер" i]',
    'input[placeholder*="phone" i]',
    'input[placeholder*="+380" i]',
    'input[placeholder*="+7" i]',
]
NAME_SEL = [
    'input[name*="name" i]',
    'input[id*="name" i]',
    'input[placeholder*="имя" i]',
    'input[placeholder*="ім" i]',
    'input[placeholder*="name" i]',
]
# Тексты кнопок, которые открывают форму (рус / укр / англ)
TRIGGER_TEXTS = [
    "заказать звонок", "обратный звонок", "перезвон", "оставить заявку",
    "консультац", "замовити дзвінок", "зворотний дзвінок", "передзвон",
    "залишити заявку", "request a call", "call back", "callback",
]
SUBMIT_TEXTS = [
    "отправ", "заказ", "перезвон", "жду звонка", "отримати", "надісл",
    "відправ", "замовити", "передзвон", "send", "submit", "request",
]
COOKIE_TEXTS = ["принять", "согласен", "прийняти", "погоджуюсь", "accept", "agree", "ok"]
SUCCESS_WORDS = [
    "спасибо", "дякуємо", "дякую", "заявка принята", "заявка отправлена",
    "заявку прийнято", "ми зв'яжемося", "мы свяжемся", "перезвоним",
    "передзвонимо", "thank you", "thanks",
]

app = Flask(__name__)
STATE = {"running": False, "rows": []}
STOP = threading.Event()


def host_of(url):
    return urlparse(url).netloc.lower().replace("www.", "")


def normalize(url):
    url = url.strip()
    if url and not url.startswith("http"):
        url = "https://" + url
    return url


def load_done():
    try:
        return json.loads(DONE_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_done(d):
    DONE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")


def first_visible(scope, selectors):
    """Первый видимый элемент по списку селекторов (scope: Page или Locator)."""
    for sel in selectors:
        loc = scope.locator(sel)
        try:
            n = min(loc.count(), 10)
        except Exception:
            continue
        for i in range(n):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    return el
            except Exception:
                pass
    return None


def click_by_text(page, texts):
    """Кликает по первой видимой ссылке/кнопке, текст которой содержит одну из фраз."""
    clickable = page.locator("a, button, [role=button]")
    for text in texts:
        loc = clickable.filter(has_text=re.compile(re.escape(text), re.I))
        try:
            n = min(loc.count(), 6)
        except Exception:
            continue
        for i in range(n):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    el.click(timeout=3000)
                    return True
            except Exception:
                pass
    return False


def dismiss_cookies(page):
    try:
        click_by_text(page, COOKIE_TEXTS)
        page.wait_for_timeout(500)
    except Exception:
        pass


def open_form(page):
    """Если поля телефона не видно — пробуем нажать «Заказать звонок» и т.п."""
    for _ in range(2):
        if not click_by_text(page, TRIGGER_TEXTS):
            return False
        page.wait_for_timeout(2000)
        if first_visible(page, PHONE_SEL):
            return True
    return False


def has_captcha(page):
    sel = ('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], '
           '.g-recaptcha, .h-captcha, [data-sitekey]')
    try:
        return page.locator(sel).count() > 0
    except Exception:
        return False


def tick_consent(scope):
    """Ставит галочки согласия (обработка данных) внутри формы."""
    boxes = scope.locator('input[type="checkbox"]')
    try:
        n = min(boxes.count(), 5)
    except Exception:
        return
    for i in range(n):
        try:
            cb = boxes.nth(i)
            if not cb.is_checked():
                cb.check(force=True, timeout=1500)
        except Exception:
            pass


def submit(page, phone_el, scope, has_form):
    if has_form:
        btns = scope.locator('button, input[type="submit"], [role=button]')
        pattern = re.compile("|".join(SUBMIT_TEXTS), re.I)
        try:
            n = min(btns.count(), 15)
        except Exception:
            n = 0
        for i in range(n):
            b = btns.nth(i)
            try:
                if not b.is_visible():
                    continue
                label = (b.inner_text() or "") + " " + (b.get_attribute("value") or "")
                if pattern.search(label):
                    b.click(timeout=3000)
                    return
            except Exception:
                pass
        sub = first_visible(scope, ['[type="submit"]'])
        if sub:
            sub.click(timeout=3000)
            return
    phone_el.press("Enter")


def process_site(browser, url, name, phone):
    """Возвращает (статус, заметка, имя_скриншота)."""
    ctx = browser.new_context(locale="ru-RU", viewport={"width": 1366, "height": 850})
    page = ctx.new_page()
    shot = f"{host_of(url).replace(':', '_')}_{datetime.now():%H%M%S}.png"
    shot_path = str(SHOTS / shot)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        dismiss_cookies(page)

        phone_el = first_visible(page, PHONE_SEL)
        if not phone_el and open_form(page):
            phone_el = first_visible(page, PHONE_SEL)
        if not phone_el:
            page.screenshot(path=shot_path)
            return "форма не найдена", "нужна ручная настройка селекторов", shot

        if has_captcha(page):
            page.screenshot(path=shot_path)
            return "капча", "отправь вручную, бот капчу не обходит", shot

        form = phone_el.locator("xpath=ancestor::form[1]")
        has_form = form.count() > 0
        scope = form if has_form else page

        name_el = first_visible(scope, NAME_SEL)
        if name_el:
            name_el.click()
            name_el.fill(name)

        phone_el.click()
        phone_el.fill("")
        phone_el.press_sequentially(phone, delay=40)  # печатаем по символам из-за масок ввода

        if has_form:
            tick_consent(scope)
        submit(page, phone_el, scope, has_form)

        page.wait_for_timeout(4000)
        page.screenshot(path=shot_path)
        text = page.inner_text("body").lower()
        if any(w in text for w in SUCCESS_WORDS):
            return "отправлено", "", shot
        return "отправлено?", "подтверждения не видно, проверь скриншот", shot
    except Exception as e:
        try:
            page.screenshot(path=shot_path)
        except Exception:
            shot = ""
        return "ошибка", str(e).splitlines()[0][:150], shot
    finally:
        ctx.close()


def log_row(url, status, note):
    new = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["время", "сайт", "статус", "заметка"])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), url, status, note])


def run_job(name, phone, urls, ignore_history, show_browser):
    done = load_done()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not show_browser)
            for i, row in enumerate(STATE["rows"]):
                if STOP.is_set():
                    break
                url = row["url"]
                host = host_of(url)
                if host in done and not ignore_history:
                    row["status"] = "пропущен"
                    row["note"] = f"уже отправляли {done[host]}"
                    continue
                row["status"] = "в работе"
                status, note, shot = process_site(browser, url, name, phone)
                row.update(status=status, note=note, shot=shot)
                log_row(url, status, note)
                if status.startswith("отправлено"):
                    done[host] = datetime.now().strftime("%Y-%m-%d %H:%M")
                    save_done(done)
                if i < len(STATE["rows"]) - 1:
                    # ждём паузу, но просыпаемся сразу, если нажали «Стоп»
                    STOP.wait(random.uniform(*DELAY))
            browser.close()
    finally:
        for row in STATE["rows"]:
            if row["status"] in ("в очереди", "в работе") and STOP.is_set():
                row["status"] = "остановлено"
        STATE["running"] = False


@app.post("/stop")
def stop():
    STOP.set()
    return jsonify(ok=True)


@app.post("/start")
def start():
    if STATE["running"]:
        return jsonify(error="Уже работает, дождись окончания"), 400
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    urls = [normalize(u) for u in (data.get("urls") or "").splitlines() if u.strip()]
    if not name or not phone or not urls:
        return jsonify(error="Заполни имя, номер и хотя бы один сайт"), 400
    STOP.clear()
    STATE["running"] = True
    STATE["rows"] = [{"url": u, "status": "в очереди", "note": "", "shot": ""} for u in urls]
    threading.Thread(
        target=run_job,
        args=(name, phone, urls, bool(data.get("ignore")), bool(data.get("show"))),
        daemon=True,
    ).start()
    return jsonify(ok=True)


@app.get("/status")
def status():
    return jsonify(STATE)


@app.get("/shots/<path:fname>")
def shots(fname):
    return send_from_directory(SHOTS, fname)


PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Заявки на звонок</title>
<style>
body{font-family:system-ui,sans-serif;max-width:760px;margin:30px auto;padding:0 16px;color:#222}
input,textarea{width:100%;padding:10px;margin:6px 0 12px;font-size:16px;box-sizing:border-box}
button{padding:14px 26px;font-size:17px;cursor:pointer;margin-right:8px;border-radius:8px}
table{display:block;overflow-x:auto}
label.chk{display:block;margin:4px 0}
table{width:100%;border-collapse:collapse;margin-top:24px}
td,th{border-bottom:1px solid #ddd;padding:8px;text-align:left;font-size:14px;vertical-align:top}
</style></head><body>
<h2>Заявки на обратный звонок</h2>
<label>Имя</label><input id="name" placeholder="Иван">
<label>Номер</label><input id="phone" placeholder="+380501234567">
<label>Сайты (по одному в строке)</label>
<textarea id="urls" rows="8" placeholder="example.com&#10;another-site.ua"></textarea>
<label class="chk"><input type="checkbox" id="show" style="width:auto"> Показывать окно браузера</label>
<label class="chk"><input type="checkbox" id="ignore" style="width:auto"> Отправлять повторно, даже если уже отправляли</label>
<br><button onclick="start()">Запустить</button>
<button onclick="stopJob()" style="background:#c62828;color:#fff;border:0;border-radius:8px">Стоп</button>
<table><thead><tr><th>Сайт</th><th>Статус</th><th>Заметка</th><th></th></tr></thead>
<tbody id="rows"></tbody></table>
<script>
async function start(){
  const body = {
    name: document.getElementById('name').value,
    phone: document.getElementById('phone').value,
    urls: document.getElementById('urls').value,
    show: document.getElementById('show').checked,
    ignore: document.getElementById('ignore').checked
  };
  const r = await fetch('/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  if (j.error) alert(j.error);
}
async function stopJob(){
  await fetch('/stop', {method:'POST'});
}
async function poll(){
  const r = await fetch('/status');
  const j = await r.json();
  const tb = document.getElementById('rows');
  tb.textContent = '';
  for (const row of j.rows) {
    const tr = document.createElement('tr');
    for (const v of [row.url, row.status, row.note]) {
      const td = document.createElement('td'); td.textContent = v; tr.appendChild(td);
    }
    const td = document.createElement('td');
    if (row.shot) {
      const a = document.createElement('a');
      a.href = '/shots/' + row.shot; a.target = '_blank'; a.textContent = 'скриншот';
      td.appendChild(a);
    }
    tr.appendChild(td);
    tb.appendChild(tr);
  }
}
setInterval(poll, 2000);
</script></body></html>
"""


@app.get("/")
def index():
    return PAGE


def local_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # пакеты не отправляются, нужен только адрес
        return s.getsockname()[0]
    except Exception:
        return "IP-компьютера"
    finally:
        s.close()


if __name__ == "__main__":
    print("На компьютере:  http://localhost:5000")
    print(f"С телефона (тот же Wi-Fi):  http://{local_ip()}:5000")
    app.run(host="0.0.0.0", port=5000)
