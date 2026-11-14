import os
import sys
import json
import base64
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urlparse, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------- WINDOWS: кодировка консоли ----------
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------- НАСТРОЙКИ ПАРСЕРА ----------
LOGIN      = os.getenv("ZK_LOGIN", "ka")
PASSWORD   = os.getenv("ZK_PASSWORD", "")
BASE_URL   = "https://www.zayavka-karty.ru"
TARGET_URL = BASE_URL + "/ka"

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR   = SCRIPT_DIR
OUTPUT_CSV   = os.path.join(OUTPUT_DIR, "output.csv")
RECENTLY_CSV = os.path.join(OUTPUT_DIR, "recently_done.csv")
OUTPUT_HTML  = os.path.join(OUTPUT_DIR, "dashboard.html")
DEBUG_HTML   = os.path.join(OUTPUT_DIR, "_debug_page.html")

UA_STRING = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ---------- ФИЛЬТР «НЕДАВНО ГОТОВО» ----------
def _parse_since(value: str) -> datetime:
    v = value.strip()
    parts = v.split(".")
    try:
        if len(parts) == 3:
            return datetime(int(parts[2]), int(parts[1]), int(parts[0]))
        if len(parts) == 2:
            return datetime(int(parts[1]), int(parts[0]), 1)
        if len(parts) == 1 and len(parts[0]) == 4:
            return datetime(int(parts[0]), 1, 1)
    except ValueError:
        pass
    raise ValueError(f"Некорректный RECENTLY_SINCE: {value!r}")


RECENTLY_SINCE = _parse_since(
    os.getenv("RECENTLY_SINCE", f"11.{datetime.now().year}")
)

# ---------- ЯНДЕКС.ДИСК ----------
YANDEX_TOKEN         = os.getenv("YANDEX_TOKEN", "")
YANDEX_REMOTE_FOLDER = os.getenv("YANDEX_REMOTE_FOLDER", "disk:/__smart_place__/maps/2026")
YANDEX_API           = "https://cloud-api.yandex.net/v1/disk/resources"
YANDEX_PUBLIC_API    = "https://cloud-api.yandex.net/v1/disk/public/resources"

STALE_THRESHOLD_DAYS = int(os.getenv("STALE_THRESHOLD_DAYS", "7"))
PUBLIC_API_WORKERS   = int(os.getenv("PUBLIC_API_WORKERS", "6"))
PUBLIC_API_TIMEOUT   = int(os.getenv("PUBLIC_API_TIMEOUT", "15"))

# ---------- НАСТРОЙКИ GITHUB ----------
GITHUB_TOKEN     = os.getenv("GITHUB_TOKEN", "")
GITHUB_USERNAME  = os.getenv("GITHUB_USERNAME", "SpritePetrovich")
GITHUB_REPO      = os.getenv("GITHUB_REPO", "monitoring-dashboard")
GITHUB_FILE_PATH = "index.html"
GITHUB_BRANCH    = os.getenv("GITHUB_BRANCH", "main")


# =========================================================
# ================== GITHUB PAGES =========================
# =========================================================
def gh_log(msg):
    print(f"[GH {datetime.now().strftime('%H:%M:%S')}] {msg}")


def github_upload_dashboard(local_html_path):
    print("\n=== Выгрузка на GitHub Pages ===")

    if not GITHUB_TOKEN:
        gh_log("❌ GITHUB_TOKEN не задан. Пропускаем.")
        return False
    if not GITHUB_USERNAME:
        gh_log("❌ GITHUB_USERNAME не задан. Пропускаем.")
        return False
    if not os.path.exists(local_html_path):
        gh_log(f"❌ Локальный HTML-файл не найден: {local_html_path}")
        return False

    api_url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_USERNAME}/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
    )
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    try:
        with open(local_html_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        gh_log(f"❌ Ошибка чтения файла: {e}")
        return False

    content_encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    try:
        r = requests.get(api_url, headers=headers, timeout=20)
    except Exception as e:
        gh_log(f"❌ Сеть при проверке файла: {e}")
        return False

    sha = None
    if r.status_code == 200:
        sha = r.json().get("sha")
        gh_log("📄 Файл уже существует — будет обновлён.")
    elif r.status_code == 404:
        gh_log("🆕 Файла ещё нет — будет создан.")
    elif r.status_code == 401:
        gh_log("❌ 401 Unauthorized — токен неверный или истёк.")
        return False
    elif r.status_code == 403:
        gh_log("❌ 403 Forbidden — нет прав или исчерпан лимит.")
        return False
    else:
        gh_log(f"⚠️ Проверка файла: {r.status_code} {r.text[:200]}")

    data = {
        "message": f"Auto-update dashboard: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        "content": content_encoded,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        data["sha"] = sha

    try:
        r = requests.put(api_url, headers=headers, json=data, timeout=30)
    except Exception as e:
        gh_log(f"❌ Сеть при загрузке: {e}")
        return False

    if r.status_code in (200, 201):
        gh_log(f"✅ Файл '{GITHUB_FILE_PATH}' выгружен в '{GITHUB_REPO}'.")
        gh_log(f"🔗 Дашборд: https://{GITHUB_USERNAME}.github.io/{GITHUB_REPO}/")
        return True

    if r.status_code == 409:
        gh_log("❌ 409 Conflict — SHA устарел, попробуйте ещё раз.")
    elif r.status_code == 422:
        gh_log(f"❌ 422 — вероятно, неверная ветка '{GITHUB_BRANCH}'.")
    else:
        gh_log(f"❌ Ошибка загрузки: {r.status_code} {r.text[:300]}")
    return False


# =========================================================
# ================== КЕШ ПАПКИ НА ДИСКЕ ===================
# =========================================================
def _ya_headers():
    if not YANDEX_TOKEN:
        raise RuntimeError("YANDEX_TOKEN не задан (проверь секреты на GitHub)")
    return {"Authorization": f"OAuth {YANDEX_TOKEN}"}


def load_folder_cache():
    cache = {}
    api_calls = {"n": 0}

    def walk(path, rel_prefix=""):
        params = {
            "path": path,
            "limit": 1000,
            "fields": "_embedded.items.name,_embedded.items.type,_embedded.items.modified,"
                      "_embedded.items.created,_embedded.items.path,"
                      "_embedded.total,_embedded.offset,_embedded.limit",
        }
        try:
            r = requests.get(YANDEX_API, headers=_ya_headers(), params=params, timeout=30)
            api_calls["n"] += 1
        except Exception as e:
            print(f"❌ Сеть при обходе {path}: {e}")
            return

        if r.status_code == 404:
            return
        if r.status_code != 200:
            print(f"❌ API обход {path}: {r.status_code} {r.text[:200]}")
            return

        data = r.json()
        emb = data.get("_embedded", {}) or {}
        items = emb.get("items", [])
        total = emb.get("total", 0)

        for item in items:
            name = item.get("name", "")
            typ  = item.get("type", "")
            if typ == "file":
                rel = f"{rel_prefix}/{name}".lstrip("/")
                modified = item.get("modified") or item.get("created")
                cache.setdefault(name, []).append((rel, modified))
            elif typ == "dir":
                rel = f"{rel_prefix}/{name}".lstrip("/")
                walk(item["path"], rel)

        offset = len(items)
        while offset < total:
            params["offset"] = offset
            try:
                r = requests.get(YANDEX_API, headers=_ya_headers(), params=params, timeout=30)
                api_calls["n"] += 1
            except Exception:
                break
            if r.status_code != 200:
                break
            emb = r.json().get("_embedded", {}) or {}
            items = emb.get("items", [])
            for item in items:
                name = item.get("name", "")
                typ  = item.get("type", "")
                if typ == "file":
                    rel = f"{rel_prefix}/{name}".lstrip("/")
                    modified = item.get("modified") or item.get("created")
                    cache.setdefault(name, []).append((rel, modified))
                elif typ == "dir":
                    rel = f"{rel_prefix}/{name}".lstrip("/")
                    walk(item["path"], rel)
            offset += len(items)

    walk(YANDEX_REMOTE_FOLDER)
    total_files = sum(len(v) for v in cache.values())
    print(f"📂 Кеш папки: {total_files} файлов, запросов: {api_calls['n']}")
    return cache


# =========================================================
# ============ ИМЯ ФАЙЛА ИЗ ССЫЛКИ ========================
# =========================================================
def extract_filename_from_url(link):
    if not link:
        return None
    p = urlparse(link)
    parts = [x for x in p.path.split("/") if x]
    if len(parts) >= 5 and parts[0] == "d":
        return unquote(parts[-1])
    if len(parts) == 2 and parts[0] == "d":
        return None
    return None


def get_public_name(link, timeout=PUBLIC_API_TIMEOUT):
    try:
        r = requests.get(
            YANDEX_PUBLIC_API,
            params={"public_key": link, "path": "/"},
            timeout=timeout,
            headers={"User-Agent": UA_STRING, "Accept": "application/json"},
        )
        if r.status_code == 200:
            j = r.json()
            if j.get("type") == "file":
                return j.get("name")
    except Exception:
        pass
    return None


def resolve_name(link):
    if not link:
        return None
    name = extract_filename_from_url(link)
    if not name:
        name = get_public_name(link)
    return name


def find_in_cache(name, folder_cache):
    if name not in folder_cache:
        return None, None
    entries = folder_cache[name]
    entries.sort(key=lambda x: x[1] or "", reverse=True)
    relpath, modified_iso = entries[0]
    file_dt_str = ""
    if modified_iso:
        try:
            dt = datetime.fromisoformat(modified_iso.replace("Z", "+00:00"))
            file_dt_str = dt.strftime("%d.%m.%Y %H:%M")
        except Exception:
            pass
    return file_dt_str, relpath


# =========================================================
# ============ ПАРСИНГ ДАТ ================================
# =========================================================
def parse_date_any(s):
    """Парсит дату в форматах DD.MM.YYYY, DD.MM.YYYY HH:MM, DD.MM.YYYY HH:MM:SS."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# =========================================================
# ============ ПРОВЕРКА ГОТОВОЙ ЗАЯВКИ ====================
# =========================================================
def evaluate_recent(link, date_done_str, folder_cache):
    if not link:
        return {"status": "no_link", "reason": "нет ссылки", "file_dt": None, "name": None}

    name = resolve_name(link)
    if not name:
        return {"status": "link_broken", "reason": "не удалось определить имя файла",
                "file_dt": None, "name": None}

    if name not in folder_cache:
        return {"status": "not_in_folder", "reason": "нет в папке maps/2026",
                "file_dt": None, "name": name}

    file_dt_str, _ = find_in_cache(name, folder_cache)
    if not file_dt_str:
        return {"status": "unknown", "reason": "нет даты у файла",
                "file_dt": None, "name": name}

    try:
        file_dt = datetime.strptime(file_dt_str, "%d.%m.%Y %H:%M")
    except ValueError:
        return {"status": "unknown", "reason": "дата не парсится",
                "file_dt": file_dt_str, "name": name}

    date_done = parse_date_any(date_done_str)
    if date_done is None:
        return {"status": "unknown", "reason": "нет даты заявки",
                "file_dt": file_dt_str, "name": name}

    delta_days = (file_dt.date() - date_done.date()).days

    if delta_days < -STALE_THRESHOLD_DAYS:
        return {
            "status": "stale",
            "reason": f"загружен за {abs(delta_days)} дн. до заявки",
            "file_dt": file_dt_str,
            "name": name,
        }

    return {"status": "actual", "reason": "ok",
            "file_dt": file_dt_str, "name": name}


# =========================================================
# ============ ПРОВЕРКА АКТИВНОЙ ЗАЯВКИ ===================
# =========================================================
def evaluate_active(link, app_date_str, folder_cache):
    """
    Проверяет активную заявку:
    1) файл есть в папке maps/2026?
    2) дата файла не старше даты заявки более чем на STALE_THRESHOLD_DAYS?
    """
    if not link:
        return {"status": "no_link", "reason": "нет ссылки", "file_dt": None, "name": None}

    name = resolve_name(link)
    if not name:
        return {"status": "link_broken", "reason": "не удалось определить имя файла",
                "file_dt": None, "name": None}

    if name not in folder_cache:
        return {"status": "not_in_folder", "reason": "нет в папке maps/2026",
                "file_dt": None, "name": name}

    file_dt_str, _ = find_in_cache(name, folder_cache)
    if not file_dt_str:
        return {"status": "unknown", "reason": "нет даты у файла",
                "file_dt": None, "name": name}

    try:
        file_dt = datetime.strptime(file_dt_str, "%d.%m.%Y %H:%M")
    except ValueError:
        return {"status": "unknown", "reason": "дата не парсится",
                "file_dt": file_dt_str, "name": name}

    # Сравнение с датой заявки
    app_date = parse_date_any(app_date_str)
    if app_date is None:
        # Не удалось распарсить дату заявки — только проверка наличия
        return {"status": "actual", "reason": "ok (без сравнения дат)",
                "file_dt": file_dt_str, "name": name}

    delta_days = (file_dt.date() - app_date.date()).days

    if delta_days < -STALE_THRESHOLD_DAYS:
        return {
            "status": "stale",
            "reason": f"загружен за {abs(delta_days)} дн. до заявки",
            "file_dt": file_dt_str,
            "name": name,
        }

    return {"status": "actual", "reason": "ok",
            "file_dt": file_dt_str, "name": name}


# =========================================================
# ============ ПАРАЛЛЕЛЬНАЯ ПРОВЕРКА ======================
# =========================================================
def evaluate_all_recent(recently_filtered, folder_cache):
    results = {}

    def eval_one(row):
        num, date, sam, link, dt = row
        return num, evaluate_recent(link, date, folder_cache)

    print(f"\n=== Проверка {len(recently_filtered)} готовых заявок ===")
    with ThreadPoolExecutor(max_workers=PUBLIC_API_WORKERS) as ex:
        futures = {ex.submit(eval_one, row): row for row in recently_filtered}
        for i, f in enumerate(as_completed(futures), 1):
            num, res = f.result()
            results[num] = res
            icon = {
                "actual": "✅", "stale": "❌", "not_in_folder": "❌",
                "link_broken": "❌", "no_link": "⚪", "unknown": "⚪",
            }.get(res["status"], "?")
            print(f"   [{i}/{len(futures)}] {icon} {num:<5} {res['status']:<14} "
                  f"{(res['file_dt'] or ''):<18} {res['reason']}")
    return results


def evaluate_all_active(active_data, folder_cache):
    """Проверяет активные заявки, у которых в 'Карта' есть ссылка."""
    to_check = [row for row in active_data if row[5]]  # link на индексе 5
    results = {}

    if not to_check:
        print("\n⏭ Активных со ссылкой нет — сверка не требуется")
        return results

    print(f"\n=== Проверка {len(to_check)} активных заявок со ссылкой ===")

    def eval_one(row):
        num, city, sam, status, reason, link, app_date = row
        return num, evaluate_active(link, app_date, folder_cache)

    with ThreadPoolExecutor(max_workers=PUBLIC_API_WORKERS) as ex:
        futures = {ex.submit(eval_one, row): row for row in to_check}
        for i, f in enumerate(as_completed(futures), 1):
            num, res = f.result()
            results[num] = res
            icon = {
                "actual": "✅", "stale": "❌",
                "not_in_folder": "❌", "link_broken": "❌",
            }.get(res["status"], "?")
            print(f"   [{i}/{len(futures)}] {icon} {num:<5} {res['status']:<14} "
                  f"{(res['file_dt'] or ''):<18} {res['reason']}")
    return results


# =========================================================
# ================== АВТОРИЗАЦИЯ ==========================
# =========================================================
def login(session):
    login_url = BASE_URL + "/login"
    print(f"\n=== Авторизация: GET {login_url} ===")
    try:
        resp = session.get(login_url)
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ GET /login: {e}")
        return False

    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form")
    if not form:
        print("⚠️ Формы нет — пробуем Basic Auth")
        session.auth = (LOGIN, PASSWORD)
        return session.get(TARGET_URL).status_code == 200

    data, uf, pf = {}, None, None
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        if inp.get("type") == "hidden":
            data[name] = inp.get("value", "")
        if inp.get("type") in ("text", "email") or "login" in name.lower() or "user" in name.lower():
            uf = name
        if inp.get("type") == "password" or "pass" in name.lower():
            pf = name

    if not uf or not pf:
        print(f"❌ Не нашли поля: login={uf}, password={pf}")
        return False

    data[uf] = LOGIN
    data[pf] = PASSWORD

    action = form.get("action") or login_url
    if not action.startswith("http"):
        action = BASE_URL + action

    print(f"=== POST {action} ===")
    resp = session.post(action, data=data)
    print(f"   статус {resp.status_code}, URL {resp.url}")

    if "/login" in resp.url:
        print("❌ Не пустило.")
        return False
    print("✅ Авторизация прошла.")
    return True


# =========================================================
# ==================== ПАРСИНГ ============================
# =========================================================
def _parse_active_table(table, headers):
    """
    Активные: (num, city, sam, status, reason, link, app_date_str).
    app_date берём из столбца «Дата заявки КА».
    """
    def idx(*names):
        for n in names:
            if n in headers:
                return headers.index(n)
        return None

    i_num    = idx("#")
    i_date   = idx("Дата заявки КА")
    i_city   = idx("Город")
    i_sam    = idx("Название SAM")
    i_status = idx("Карта")
    i_reason = idx("Причина")

    if i_city is None or i_sam is None:
        return []
    if i_num is None:
        i_num = 0

    tbody = table.find("tbody")
    if not tbody:
        return []

    max_idx = max(
        i_num, i_city, i_sam,
        i_status if i_status is not None else 0,
        i_reason if i_reason is not None else 0,
        i_date   if i_date   is not None else 0,
    )

    rows = []
    for row in tbody.find_all("tr"):
        cols = row.find_all("td")
        if len(cols) <= max_idx:
            continue
        num      = cols[i_num].get_text(strip=True)
        app_date = cols[i_date].get_text(strip=True) if i_date is not None else ""
        city     = cols[i_city].get_text(strip=True)
        sam      = cols[i_sam].get_text(strip=True)
        reason   = cols[i_reason].get_text(strip=True) if i_reason is not None else ""

        link = ""
        has_on_disk = False
        if i_status is not None:
            cell = cols[i_status]
            a = cell.find("a")
            if a:
                link = a.get("href", "") or ""
            if "на диске" in cell.get_text(strip=True).lower():
                has_on_disk = True

        if link:
            status = "has_link"
        elif has_on_disk:
            status = "done"
        else:
            status = "pending"

        if num and city and sam:
            rows.append((num, city, sam, status, reason, link, app_date))
    return rows


def _parse_recently_done_table(table, headers):
    def idx(name):
        return headers.index(name) if name in headers else None

    i_num  = idx("#")
    i_date = idx("Готово")
    i_sam  = idx("SAM")
    i_link = idx("Ссылка")

    if i_sam is None:
        return []

    tbody = table.find("tbody")
    if not tbody:
        return []

    max_idx = max(x for x in [i_num or 0, i_date or 0, i_sam, i_link or 0])

    rows = []
    for row in tbody.find_all("tr"):
        cols = row.find_all("td")
        if len(cols) <= max_idx:
            continue
        num  = cols[i_num].get_text(strip=True) if i_num is not None else ""
        date = cols[i_date].get_text(strip=True) if i_date is not None else ""
        sam  = cols[i_sam].get_text(strip=True)
        link = ""
        if i_link is not None:
            a = cols[i_link].find("a")
            if a:
                link = a.get("href", "")
        if num and sam:
            rows.append((num, date, sam, link))
    return rows


def parse_soup(soup):
    active, recently = [], []

    tables = soup.find_all("table")
    print(f"   таблиц: {len(tables)}")

    for i, table in enumerate(tables):
        thead = table.find("thead")
        if not thead:
            continue
        ths = [th.get_text(strip=True).replace("\xa0", " ") for th in thead.find_all("th")]
        print(f"     #{i}: {ths}")

        if "Город" in ths and "Название SAM" in ths:
            print(f"   ✅ активная таблица: #{i}")
            active = _parse_active_table(table, ths)
        elif "Готово" in ths and "SAM" in ths and "Ссылка" in ths:
            print(f"   ✅ таблица «Недавно готово»: #{i}")
            recently = _parse_recently_done_table(table, ths)

    return active, recently


def fetch_and_parse(session, url):
    print(f"\n=== Парсинг {url} ===")
    try:
        resp = session.get(url)
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ GET {url}: {e}")
        return [], []

    print(f"   статус {resp.status_code}, длина {len(resp.text)}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(DEBUG_HTML, "w", encoding="utf-8") as f:
        f.write(resp.text)

    soup = BeautifulSoup(resp.text, "html.parser")
    active, recently = parse_soup(soup)

    if not active and not recently:
        print("\n>>> Пробуем авторизоваться.")
        if login(session):
            resp = session.get(url)
            with open(DEBUG_HTML, "w", encoding="utf-8") as f:
                f.write(resp.text)
            soup = BeautifulSoup(resp.text, "html.parser")
            active, recently = parse_soup(soup)

    return active, recently


# =========================================================
# ==================== ДАТА ===============================
# =========================================================
def parse_date_dmy(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%d.%m.%Y")
    except (ValueError, AttributeError):
        return None


def filter_recently_by_date(rows, since: datetime):
    kept, dropped = [], 0
    for num, date_str, sam, link in rows:
        d = parse_date_dmy(date_str)
        if d is None or d < since:
            dropped += 1
            continue
        kept.append((num, date_str, sam, link, d))
    return kept, dropped


# =========================================================
# ==================== CSV ================================
# =========================================================
def save_active_csv(data, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("num;city;sam;status;reason;link;app_date\n")
        for num, city, sam, status, reason, link, app_date in data:
            link_esc = (link or "").replace(";", "%3B")
            f.write(f"{num};{city};{sam};{status};{reason};{link_esc};{app_date}\n")
    print(f"💾 CSV (активные): {path}")


def save_recently_csv(rows, eval_results, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("num;date_done;sam;link;file_in_folder_date;status;reason\n")
        for num, date, sam, link, _ in rows:
            ev = eval_results.get(num, {})
            link_esc = (link or "").replace(";", "%3B")
            reason_esc = (ev.get("reason") or "").replace(";", ",")
            f.write(
                f"{num};{date};{sam};{link_esc};"
                f"{ev.get('file_dt') or ''};{ev.get('status') or ''};{reason_esc}\n"
            )
    print(f"💾 CSV (недавно готово): {path}")


# =========================================================
# ==================== HTML ===============================
# =========================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Заявки в работе</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f5f6f8; color: #1a1d24; padding: 24px; line-height: 1.5; }
  .container { max-width: 1180px; margin: 0 auto; }
  h1 { font-size: 24px; margin-bottom: 4px; }
  h2 { font-size: 17px; margin: 28px 0 12px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .badge-count {
    display: inline-block; padding: 2px 10px; border-radius: 999px;
    background: #e0e7ff; color: #3730a3; font-size: 12px; font-weight: 700;
  }
  .badge-count.red { background: #fee2e2; color: #991b1b; }
  .updated { color: #6b7280; font-size: 13px; margin-bottom: 24px; }

  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
           gap: 12px; margin-bottom: 24px; }
  .stat { background: #fff; border-radius: 12px; padding: 20px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.06); text-align: center; }
  .stat .value { font-size: 36px; font-weight: 700; }
  .stat .label { font-size: 12px; color: #6b7280; margin-top: 4px; }
  .stat.active   .value { color: #dc2626; }
  .stat.recently .value { color: #2563eb; }
  .stat.broken   .value { color: #dc2626; }
  .stat.ok       .value { color: #16a34a; }

  table { width: 100%; border-collapse: collapse; background: #fff;
          border-radius: 12px; overflow: hidden;
          box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  th, td { padding: 12px 16px; text-align: left; border-bottom: 1px solid #f0f0f0; }
  th { background: #fafafa; font-size: 12px; text-transform: uppercase;
       color: #6b7280; font-weight: 600; }
  tr:last-child td { border-bottom: none; }
  td.num { text-align: center; font-variant-numeric: tabular-nums; color: #6b7280; }
  td.upload-date { font-variant-numeric: tabular-nums; white-space: nowrap; }
  td.upload-date.bad { color: #dc2626; font-weight: 600; }
  td.upload-date.unknown { color: #9ca3af; }

  a.file-link { color: #2563eb; text-decoration: none; font-weight: 500; }
  a.file-link:hover { text-decoration: underline; }
  a.file-link.broken { color: #dc2626; text-decoration: line-through; }

  .status { display: inline-flex; align-items: center; gap: 6px;
            font-size: 12px; font-weight: 600; }
  .status .dot { width: 8px; height: 8px; border-radius: 50%; }
  .status.ok .dot      { background: #16a34a; }
  .status.ok          { color: #16a34a; }
  .status.bad .dot     { background: #dc2626; }
  .status.bad         { color: #dc2626; }
  .status.unknown .dot { background: #9ca3af; }
  .status.unknown     { color: #9ca3af; }

  tr.row-broken td { background: #fef2f2; }
  .scroll-wrap { max-height: 700px; overflow-y: auto; border-radius: 12px; }
  .scroll-wrap table { box-shadow: none; }
  .since-info { font-size: 13px; color: #6b7280; margin-top: 4px; }
  .empty { padding: 24px; text-align: center; color: #9ca3af;
           background: #fff; border-radius: 12px;
           box-shadow: 0 1px 3px rgba(0,0,0,0.06); }

  @media (max-width: 500px) {
    body { padding: 12px; }
    .stat .value { font-size: 28px; }
    th, td { padding: 8px 10px; font-size: 13px; }
  }
</style>
</head>
<body>
<div class="container">
  <h1>Заявки в работе</h1>
  <div class="updated">Обновлено: <span id="updated"></span></div>

  <div class="stats">
    <div class="stat active">
      <div class="value" id="total">0</div>
      <div class="label">Активных заявок</div>
    </div>
    <div class="stat ok">
      <div class="value" id="activeOk">0</div>
      <div class="label">Активных с файлом</div>
    </div>
    <div class="stat broken">
      <div class="value" id="activeProblems">0</div>
      <div class="label">Активных с проблемой</div>
    </div>
    <div class="stat recently">
      <div class="value" id="recently">0</div>
      <div class="label">Готово с <span id="sinceDate"></span></div>
    </div>
    <div class="stat ok">
      <div class="value" id="recentOk">0</div>
      <div class="label">Актуальных файлов</div>
    </div>
    <div class="stat broken">
      <div class="value" id="recentProblems">0</div>
      <div class="label">Проблемных готовых</div>
    </div>
  </div>

  <h2>
    Активные заявки со ссылкой в «Карта»
    <span class="badge-count" id="activeLinkCount">0</span>
    <span class="badge-count red" id="activeBrokenBadge" style="display:none">проблемных: 0</span>
  </h2>
  <div class="since-info">
    Файл должен быть в папке <b>maps/2026</b> и загружен не более чем за
    <b><span id="staleDays1"></span></b> дн. до даты заявки.
  </div>
  <div class="scroll-wrap">
    <table>
      <thead><tr>
        <th style="width:70px;text-align:center">#</th>
        <th>Город</th>
        <th>Название SAM</th>
        <th style="width:130px">Дата заявки</th>
        <th style="width:160px">Файл в maps/2026</th>
        <th style="width:220px">Статус</th>
      </tr></thead>
      <tbody id="activeBody"></tbody>
    </table>
  </div>

  <h2>
    Готовые заявки
    <span class="badge-count" id="recentlyCount">0</span>
    <span class="badge-count red" id="recentBrokenBadge" style="display:none">проблемных: 0</span>
  </h2>
  <div class="since-info">
    Показаны заявки, готовые начиная с <b id="sinceDate2"></b>.
    Актуальным считается файл, который есть в папке <b>maps/2026</b> и загружен
    не более чем за <b><span id="staleDays2"></span></b> дн. до даты заявки.
  </div>
  <div class="scroll-wrap">
    <table>
      <thead><tr>
        <th style="width:70px;text-align:center">#</th>
        <th style="width:110px">Готово</th>
        <th>Название</th>
        <th style="width:160px">Файл в maps/2026</th>
        <th style="width:220px">Статус</th>
      </tr></thead>
      <tbody id="recentBody"></tbody>
    </table>
  </div>
</div>

<script>
  const DATA = __DATA__;

  document.getElementById('updated').textContent       = DATA.updated;
  document.getElementById('total').textContent         = DATA.total;
  document.getElementById('recently').textContent      = DATA.recently_total;
  document.getElementById('recentlyCount').textContent = DATA.recently_total;
  document.getElementById('sinceDate').textContent     = DATA.since_date;
  document.getElementById('sinceDate2').textContent    = DATA.since_date;
  document.getElementById('staleDays1').textContent    = DATA.stale_days;
  document.getElementById('staleDays2').textContent    = DATA.stale_days;
  document.getElementById('activeLinkCount').textContent = DATA.active_with_link.length;

  // ---- Активные: подсчёт ----
  const activeOkCount = DATA.active_with_link.filter(r => r.status === 'actual').length;
  const activeProblemCount = DATA.active_with_link.filter(r =>
    r.status === 'not_in_folder' || r.status === 'link_broken' || r.status === 'stale'
  ).length;
  document.getElementById('activeOk').textContent = activeOkCount;
  document.getElementById('activeProblems').textContent = activeProblemCount;
  if (activeProblemCount > 0) {
    const b = document.getElementById('activeBrokenBadge');
    b.textContent = 'проблемных: ' + activeProblemCount;
    b.style.display = 'inline-block';
  }

  // ---- Готовые: подсчёт ----
  const recentOkCount = DATA.recently_done.filter(r => r.status === 'actual').length;
  const recentProblemCount = DATA.recently_done.filter(r =>
    r.status === 'stale' || r.status === 'not_in_folder' || r.status === 'link_broken'
  ).length;
  document.getElementById('recentOk').textContent = recentOkCount;
  document.getElementById('recentProblems').textContent = recentProblemCount;
  if (recentProblemCount > 0) {
    const b = document.getElementById('recentBrokenBadge');
    b.textContent = 'проблемных: ' + recentProblemCount;
    b.style.display = 'inline-block';
  }

  const STATUS_LABEL = {
    actual:        {cls: 'ok',      text: 'актуален'},
    stale:         {cls: 'bad',     text: 'устарел'},
    not_in_folder: {cls: 'bad',     text: 'нет в папке'},
    link_broken:   {cls: 'bad',     text: 'ссылка битая'},
    no_link:       {cls: 'unknown', text: 'нет ссылки'},
    unknown:       {cls: 'unknown', text: 'не проверен'}
  };

  function renderStatus(r) {
    const st = STATUS_LABEL[r.status] || STATUS_LABEL.unknown;
    let txt = st.text;
    if (r.reason && r.status !== 'actual') txt += ' · ' + r.reason;
    return '<span class="status ' + st.cls + '"><span class="dot"></span>' + txt + '</span>';
  }

  function renderDate(r) {
    if (!r.file_dt) return '<td class="upload-date unknown">—</td>';
    const cls = (r.status === 'stale' || r.status === 'not_in_folder') ? 'bad' : '';
    return '<td class="upload-date ' + cls + '">' + r.file_dt + '</td>';
  }

  // ---- Таблица активных ----
  const activeBody = document.getElementById('activeBody');
  if (DATA.active_with_link.length === 0) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="6" class="empty">Нет активных заявок со ссылкой в «Карта»</td>';
    activeBody.appendChild(tr);
  } else {
    DATA.active_with_link.forEach(r => {
      const tr = document.createElement('tr');
      if (r.status === 'not_in_folder' || r.status === 'link_broken' || r.status === 'stale') {
        tr.className = 'row-broken';
      }
      const appDateCell = r.app_date
        ? '<td style="white-space:nowrap">' + r.app_date + '</td>'
        : '<td style="color:#9ca3af">—</td>';
      tr.innerHTML =
        '<td class="num">' + r.num + '</td>' +
        '<td>' + r.city + '</td>' +
        '<td>' + r.sam + '</td>' +
        appDateCell +
        renderDate(r) +
        '<td>' + renderStatus(r) + '</td>';
      activeBody.appendChild(tr);
    });
  }

  // ---- Таблица готовых ----
  const recentBody = document.getElementById('recentBody');
  if (DATA.recently_done.length === 0) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="5" class="empty">Нет готовых заявок с ' + DATA.since_date + '</td>';
    recentBody.appendChild(tr);
  } else {
    DATA.recently_done.forEach(r => {
      const tr = document.createElement('tr');
      if (r.status === 'stale' || r.status === 'not_in_folder' || r.status === 'link_broken') {
        tr.className = 'row-broken';
      }
      tr.innerHTML =
        '<td class="num">' + r.num + '</td>' +
        '<td>' + r.date + '</td>' +
        '<td>' + r.sam + '</td>' +
        renderDate(r) +
        '<td>' + renderStatus(r) + '</td>';
      recentBody.appendChild(tr);
    });
  }
</script>
</body>
</html>
"""


def generate_html(active_data, active_eval, recently_rows, recent_eval, since_dt, path):
    # Активные со ссылкой
    active_with_link = []
    for num, city, sam, status, reason, link, app_date in active_data:
        if status != "has_link":
            continue
        ev = active_eval.get(num, {})
        active_with_link.append({
            "num": num,
            "app_date": app_date,
            "city": city,
            "sam": sam,
            "link": link,
            "status": ev.get("status", "unknown"),
            "reason": ev.get("reason", ""),
            "file_dt": ev.get("file_dt"),
        })
    active_with_link.sort(key=lambda r: r["num"], reverse=True)

    recently_payload = []
    for num, date, sam, link, _ in recently_rows:
        ev = recent_eval.get(num, {})
        recently_payload.append({
            "num": num,
            "date": date,
            "sam": sam,
            "link": link,
            "file_dt": ev.get("file_dt"),
            "status": ev.get("status", "unknown"),
            "reason": ev.get("reason", ""),
        })

    payload = {
        "total": len(active_data),
        "recently_total": len(recently_rows),
        "recently_done": recently_payload,
        "active_with_link": active_with_link,
        "since_date": since_dt.strftime("%d.%m.%Y"),
        "stale_days": STALE_THRESHOLD_DAYS,
        "updated": datetime.now().strftime("%d.%m.%Y %H:%M"),
    }

    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"🌐 HTML: {path}")


# =========================================================
# ====================== MAIN =============================
# =========================================================
if __name__ == "__main__":
    session = requests.Session()
    session.headers.update({"User-Agent": UA_STRING})

    # 1) Кеш файлов из папки maps/2026
    if not YANDEX_TOKEN:
        print("\n❌ YANDEX_TOKEN не задан. Проверь secrets на GitHub.")
        raise SystemExit(1)
    print(f"\n=== Загрузка кеша папки {YANDEX_REMOTE_FOLDER} ===")
    folder_cache = load_folder_cache()

    # 2) Парсим сайт
    active_data, recently_raw = fetch_and_parse(session, TARGET_URL)
    if not active_data and not recently_raw:
        print("\n❌ Не удалось получить данные с сайта.")
        raise SystemExit(1)

    # 3) Фильтр «Недавно готово» по дате
    print(f"\n=== Фильтр «Недавно готово» (с {RECENTLY_SINCE.strftime('%d.%m.%Y')}) ===")
    recently_filtered, dropped = filter_recently_by_date(recently_raw, RECENTLY_SINCE)
    print(f"   всего: {len(recently_raw)}, попало: {len(recently_filtered)}, отброшено: {dropped}")
    recently_filtered.sort(key=lambda r: r[4], reverse=True)

    # 4) Проверка активных со ссылкой
    active_eval = evaluate_all_active(active_data, folder_cache)

    # 5) Проверка готовых
    recent_eval = evaluate_all_recent(recently_filtered, folder_cache)

    # 6) Сохранение + рендер
    save_active_csv(active_data, OUTPUT_CSV)
    save_recently_csv(recently_filtered, recent_eval, RECENTLY_CSV)
    generate_html(active_data, active_eval, recently_filtered, recent_eval, RECENTLY_SINCE, OUTPUT_HTML)

    # 7) Выгрузка на GitHub
    try:
        github_upload_dashboard(OUTPUT_HTML)
    except Exception as e:
        gh_log(f"❌ Синхронизация с GitHub упала: {e}")

    # 8) Итоги
    def count_recent(st): return sum(1 for r in recent_eval.values() if r["status"] == st)
    def count_active(st): return sum(1 for r in active_eval.values() if r["status"] == st)
    active_with_link_n = sum(1 for r in active_data if r[3] == "has_link")
    active_done_n      = sum(1 for r in active_data if r[3] == "done")

    print()
    print("=" * 55)
    print(f"✅ Активных всего: {len(active_data)}")
    print(f"   ├─ со ссылкой в «Карта»: {active_with_link_n}")
    print(f"   ├─ «На Диске» (без ссылки): {active_done_n}")
    print(f"   └─ без ссылки (ожидают): {len(active_data) - active_with_link_n - active_done_n}")
    if active_with_link_n:
        print(f"      🟢 актуальных: {count_active('actual')}")
        print(f"      ❌ устарело (stale): {count_active('stale')}")
        print(f"      ❌ нет в папке: {count_active('not_in_folder')}")
        print(f"      ❌ ссылка битая: {count_active('link_broken')}")
    print()
    print(f"🔵 Готово с {RECENTLY_SINCE.strftime('%d.%m.%Y')}: {len(recently_filtered)}")
    print(f"   ├─ 🟢 актуальных: {count_recent('actual')}")
    print(f"   ├─ ❌ устарело: {count_recent('stale')}")
    print(f"   ├─ ❌ нет в папке: {count_recent('not_in_folder')}")
    print(f"   ├─ ❌ ссылка битая: {count_recent('link_broken')}")
    print(f"   └─ ⚪ не проверено: {count_recent('unknown') + count_recent('no_link')}")
    print("=" * 55)
