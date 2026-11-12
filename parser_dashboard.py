import os
import sys
import json
import base64
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from collections import defaultdict

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

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR  = SCRIPT_DIR
OUTPUT_CSV  = os.path.join(OUTPUT_DIR, "output.csv")
RECENTLY_CSV = os.path.join(OUTPUT_DIR, "recently_done.csv")
OUTPUT_HTML = os.path.join(OUTPUT_DIR, "dashboard.html")
DEBUG_HTML  = os.path.join(OUTPUT_DIR, "_debug_page.html")

PRIORITY_REASON = "Новый ЦФЗ"

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
    """Загружает локальный HTML-файл в репозиторий GitHub как index.html."""
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
    """Активные заявки: №, Город, SAM, статус, причина."""
    def idx(*names):
        for n in names:
            if n in headers:
                return headers.index(n)
        return None

    i_num    = idx("#") or 0
    i_city   = idx("Город")
    i_sam    = idx("Название SAM")
    i_status = idx("Карта")
    i_reason = idx("Причина")

    if i_city is None or i_sam is None:
        return []

    tbody = table.find("tbody")
    if not tbody:
        return []

    max_idx = max(x for x in [i_num, i_city, i_sam,
                              i_status if i_status is not None else 0,
                              i_reason if i_reason is not None else 0])

    rows = []
    for row in tbody.find_all("tr"):
        cols = row.find_all("td")
        if len(cols) <= max_idx:
            continue
        num    = cols[i_num].get_text(strip=True)
        city   = cols[i_city].get_text(strip=True)
        sam    = cols[i_sam].get_text(strip=True)
        status = "done" if (i_status is not None and "На Диске" in cols[i_status].get_text()) else "pending"
        reason = cols[i_reason].get_text(strip=True) if i_reason is not None else ""
        if num and city and sam:
            rows.append((num, city, sam, status, reason))
    return rows


def _parse_recently_done_table(table, headers):
    """Недавно готово: №, дата готовности, SAM, ссылка на файл."""
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
    """Возвращает (active_rows, recently_done_rows)."""
    active, recently_done = [], []

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
            recently_done = _parse_recently_done_table(table, ths)

    return active, recently_done


def fetch_and_parse(session, url):
    """Загружает страницу, при необходимости логинится, парсит обе таблицы."""
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
    print(f"   HTML: {DEBUG_HTML}")

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
# ==================== CSV ================================
# =========================================================
def save_active_csv(data, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("num;city;sam;status;reason\n")
        for num, city, sam, status, reason in data:
            f.write(f"{num};{city};{sam};{status};{reason}\n")
    print(f"💾 CSV (активные): {path}")


def save_recently_csv(data, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("num;date_done;sam;link\n")
        for num, date, sam, link in data:
            f.write(f"{num};{date};{sam};{link}\n")
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
  .container { max-width: 1080px; margin: 0 auto; }
  h1 { font-size: 24px; margin-bottom: 4px; }
  h2 { font-size: 17px; margin: 28px 0 12px; display: flex; align-items: center; gap: 8px; }
  .badge-count {
    display: inline-block; padding: 2px 10px; border-radius: 999px;
    background: #e0e7ff; color: #3730a3; font-size: 12px; font-weight: 700;
  }
  .updated { color: #6b7280; font-size: 13px; margin-bottom: 24px; }

  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
           gap: 12px; margin-bottom: 24px; }
  .stat { background: #fff; border-radius: 12px; padding: 18px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.06); text-align: center; }
  .stat .value { font-size: 32px; font-weight: 700; }
  .stat .label { font-size: 12px; color: #6b7280; margin-top: 4px; }
  .stat.done    .value { color: #16a34a; }
  .stat.pending .value { color: #dc2626; }
  .stat.priority { background: #fef3c7; border: 1px solid #fcd34d; }
  .stat.priority .value { color: #b45309; }
  .stat.priority .label { color: #92400e; font-weight: 600; }
  .stat.recently .value { color: #2563eb; }

  .progress-wrap { background: #fff; border-radius: 12px; padding: 16px 20px;
                   margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  .progress-header { display: flex; justify-content: space-between; font-size: 13px;
                     color: #6b7280; margin-bottom: 8px; }
  .progress { height: 10px; background: #e5e7eb; border-radius: 5px; overflow: hidden; }
  .progress-bar { height: 100%; background: linear-gradient(90deg, #16a34a, #22c55e);
                  border-radius: 5px; transition: width 0.6s ease; }

  table { width: 100%; border-collapse: collapse; background: #fff;
          border-radius: 12px; overflow: hidden;
          box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  th, td { padding: 12px 16px; text-align: left; border-bottom: 1px solid #f0f0f0; }
  th { background: #fafafa; font-size: 12px; text-transform: uppercase;
       color: #6b7280; font-weight: 600; }
  tr:last-child td { border-bottom: none; }
  td.num     { text-align: center; font-variant-numeric: tabular-nums; color: #6b7280; }
  td.done    { color: #16a34a; font-weight: 600; text-align: center; }
  td.pending { color: #dc2626; text-align: center; }

  td.priority-cell { text-align: center; }
  td.priority-cell .badge-priority {
    display: inline-block; padding: 2px 10px; border-radius: 999px;
    background: #fef3c7; color: #92400e; font-weight: 700; font-size: 13px;
    border: 1px solid #fcd34d;
  }
  .bar-mini { display: inline-block; width: 60px; height: 6px; background: #e5e7eb;
              border-radius: 3px; overflow: hidden; vertical-align: middle; margin-right: 6px; }
  .bar-mini > div { height: 100%; background: #22c55e; }

  .legend { font-size: 12px; color: #6b7280; margin-top: 8px; }
  .legend .swatch { display: inline-block; width: 12px; height: 12px;
                    background: #fef3c7; border: 1px solid #fcd34d;
                    border-radius: 3px; vertical-align: middle; margin-right: 4px; }

  a.file-link { color: #2563eb; text-decoration: none; font-weight: 500; }
  a.file-link:hover { text-decoration: underline; }

  .recently-wrap { max-height: 600px; overflow-y: auto; border-radius: 12px; }
  .recently-wrap table { box-shadow: none; }

  @media (max-width: 500px) {
    body { padding: 12px; }
    .stat .value { font-size: 24px; }
    th, td { padding: 8px 10px; font-size: 13px; }
  }
</style>
</head>
<body>
<div class="container">
  <h1>Заявки в работе</h1>
  <div class="updated">Обновлено: <span id="updated"></span></div>

  <div class="stats">
    <div class="stat">          <div class="value" id="total">0</div>   <div class="label">Всего активных</div></div>
    <div class="stat done">     <div class="value" id="done">0</div>    <div class="label">На Диске</div></div>
    <div class="stat pending">  <div class="value" id="pending">0</div> <div class="label">Ожидают</div></div>
    <div class="stat priority"> <div class="value" id="prio">0</div>    <div class="label">Приоритет · Новый ЦФЗ</div></div>
    <div class="stat recently"> <div class="value" id="recently">0</div><div class="label">Недавно готово</div></div>
    <div class="stat">          <div class="value" id="percent">0%</div><div class="label">Готово (активные)</div></div>
  </div>

  <div class="progress-wrap">
    <div class="progress-header"><span>Прогресс активных</span><span id="progressText">0 / 0</span></div>
    <div class="progress"><div class="progress-bar" id="progressBar"></div></div>
  </div>

  <h2>По городам</h2>
  <table>
    <thead><tr>
      <th>Город</th>
      <th style="text-align:center">Всего</th>
      <th style="text-align:center">На Диске</th>
      <th style="text-align:center">Ожидают</th>
      <th style="text-align:center">Причина · Новый ЦФЗ</th>
      <th>Прогресс</th>
    </tr></thead>
    <tbody id="cityBody"></tbody>
  </table>
  <div class="legend"><span class="swatch"></span>жёлтым отмечено количество заявок с причиной «Новый ЦФЗ» — они приоритетны к исполнению</div>

  <h2>Недавно готово <span class="badge-count" id="recentlyCount">0</span></h2>
  <div class="recently-wrap">
    <table>
      <thead><tr>
        <th style="width:70px;text-align:center">#</th>
        <th style="width:120px">Дата готовности</th>
        <th>SAM</th>
        <th style="width:110px;text-align:center">Файл</th>
      </tr></thead>
      <tbody id="recentlyBody"></tbody>
    </table>
  </div>
</div>

<script>
  const DATA = __DATA__;
  document.getElementById('updated').textContent      = DATA.updated;
  document.getElementById('total').textContent        = DATA.total;
  document.getElementById('done').textContent         = DATA.done;
  document.getElementById('pending').textContent      = DATA.pending;
  document.getElementById('prio').textContent         = DATA.priority_total;
  document.getElementById('percent').textContent      = DATA.percent + '%';
  document.getElementById('progressText').textContent = DATA.done + ' / ' + DATA.total;
  document.getElementById('progressBar').style.width  = DATA.percent + '%';
  document.getElementById('recently').textContent     = DATA.recently_total;
  document.getElementById('recentlyCount').textContent = DATA.recently_total;

  // ---- По городам ----
  const body = document.getElementById('cityBody');
  DATA.cities.forEach(c => {
    const pct = c.total ? Math.round(c.done / c.total * 100) : 0;
    const prioCell = c.priority > 0
      ? '<span class="badge-priority">' + c.priority + '</span>'
      : '<span style="color:#9ca3af">—</span>';
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td>' + c.city + '</td>' +
      '<td class="num">' + c.total + '</td>' +
      '<td class="done">' + c.done + '</td>' +
      '<td class="' + (c.pending > 0 ? 'pending' : 'num') + '">' + c.pending + '</td>' +
      '<td class="priority-cell">' + prioCell + '</td>' +
      '<td><span class="bar-mini"><div style="width:' + pct + '%"></div></span>' + pct + '%</td>';
    body.appendChild(tr);
  });

  // ---- Недавно готово ----
  const rbody = document.getElementById('recentlyBody');
  DATA.recently_done.forEach(r => {
    const linkCell = r.link
      ? '<a class="file-link" href="' + r.link + '" target="_blank" rel="noopener">открыть ↗</a>'
      : '<span style="color:#9ca3af">—</span>';
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td class="num">' + r.num + '</td>' +
      '<td>' + r.date + '</td>' +
      '<td>' + r.sam + '</td>' +
      '<td style="text-align:center">' + linkCell + '</td>';
    rbody.appendChild(tr);
  });
</script>
</body>
</html>
"""


def generate_html(active_data, recently_data, path):
    # --- Статистика по активным ---
    total   = len(active_data)
    done    = sum(1 for r in active_data if r[3] == "done")
    pending = total - done
    percent = round(done * 100 / total) if total else 0

    def is_priority(reason, status):
        return status == "pending" and PRIORITY_REASON.lower() in reason.lower()

    priority_total = sum(1 for r in active_data if is_priority(r[4], r[3]))

    by_city = defaultdict(lambda: {"total": 0, "done": 0, "priority": 0})
    for _, city, _, status, reason in active_data:
        by_city[city]["total"] += 1
        if status == "done":
            by_city[city]["done"] += 1
        if is_priority(reason, status):
            by_city[city]["priority"] += 1

    cities = [
        {
            "city": c,
            "total": v["total"],
            "done": v["done"],
            "pending": v["total"] - v["done"],
            "priority": v["priority"],
        }
        for c, v in sorted(by_city.items())
    ]

    # --- Список недавно готовых ---
    recently_payload = [
        {"num": num, "date": date, "sam": sam, "link": link}
        for num, date, sam, link in recently_data
    ]

    payload = {
        "total": total, "done": done, "pending": pending, "percent": percent,
        "priority_total": priority_total,
        "recently_total": len(recently_data),
        "cities": cities,
        "recently_done": recently_payload,
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
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    })

    active_data, recently_data = fetch_and_parse(session, TARGET_URL)

    if not active_data and not recently_data:
        print("\n❌ Не удалось получить данные.")
        raise SystemExit(1)

    save_active_csv(active_data, OUTPUT_CSV)
    save_recently_csv(recently_data, RECENTLY_CSV)
    generate_html(active_data, recently_data, OUTPUT_HTML)

    # --- ВЫГРУЗКА НА GITHUB PAGES ---
    try:
        github_upload_dashboard(OUTPUT_HTML)
    except Exception as e:
        gh_log(f"❌ Синхронизация с GitHub упала: {e}")

    # --- ИТОГИ ---
    total   = len(active_data)
    done    = sum(1 for r in active_data if r[3] == "done")
    pending = total - done
    percent = round(done * 100 / total) if total else 0
    prio    = sum(1 for r in active_data
                  if r[3] == "pending" and PRIORITY_REASON.lower() in r[4].lower())

    print()
    print("=" * 55)
    print(f"✅ Активных: {total}")
    print(f"🟢 На Диске: {done} ({percent}%)")
    print(f"🔴 Ожидают: {pending}")
    print(f"🟡 Приоритет (Новый ЦФЗ): {prio}")
    print(f"🔵 Недавно готово: {len(recently_data)}")
    print("=" * 55)