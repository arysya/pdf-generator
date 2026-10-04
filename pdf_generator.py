"""PDF-чек-мейкер: берёт данные из CSV/JSON, подставляет в HTML-шаблон и сохраняет PDF.

Запуск:  python pdf_generator.py
Папки:   data/ (CSV и JSON), templates/ (HTML-шаблоны), output/ (готовые PDF).
"""

import csv
import io
import json
import logging
import os
import re
import subprocess
import sys
from collections import OrderedDict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError, UndefinedError

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TEMPLATES_DIR = BASE_DIR / "templates"
OUTPUT_DIR = BASE_DIR / "output"
FONTS_DIR = BASE_DIR / "fonts"

# Колонки CSV, которые относятся к позиции чека (товару или услуге).
# Остальные колонки считаются общими для всего чека: номер, дата, клиент.
ITEM_FIELDS = ("product", "qty", "price")

# Шрифт с кириллицей подключаем из папки fonts, чтобы PDF не зависел от шрифтов системы.
FONT_CSS = f"""
@font-face {{ font-family: 'Golos'; font-weight: 400;
  src: url('{(FONTS_DIR / "GolosText-Regular.ttf").as_uri()}'); }}
@font-face {{ font-family: 'Golos'; font-weight: 600;
  src: url('{(FONTS_DIR / "GolosText-SemiBold.ttf").as_uri()}'); }}
body {{ font-family: 'Golos', 'DejaVu Sans', 'Arial', sans-serif; }}
"""

# Подсказываем WeasyPrint, где лежит GTK, даже если окно запущено без обновлённого PATH.
_GTK_BIN = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GTK3-Runtime Win64" / "bin"
if sys.platform.startswith("win") and _GTK_BIN.is_dir():
    os.environ.setdefault("WEASYPRINT_DLL_DIRECTORIES", str(_GTK_BIN))

# WeasyPrint и его помощники пишут в консоль много служебных сообщений.
logging.getLogger("weasyprint").setLevel(logging.ERROR)
logging.getLogger("fontTools").setLevel(logging.ERROR)


def init_dirs():
    for folder in (DATA_DIR, TEMPLATES_DIR, OUTPUT_DIR):
        folder.mkdir(exist_ok=True)


def list_files(folder, extensions):
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in extensions)


def to_number(value, field, record_id, default=None):
    """Превращает «1 500,50 ₽» или «1500.5» в число. Ошибку показывает с номером чека."""
    text = re.sub(r"[\s  ₽]", "", str(value)).replace(",", ".")
    if not text and default is not None:
        return Decimal(default)
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"Чек {record_id}: в поле «{field}» не число: {value!r}")
    if not number.is_finite() or number < 0:
        raise ValueError(f"Чек {record_id}: в поле «{field}» странное число: {value!r}")
    return number


def round_money(value):
    return value.quantize(Decimal("0.01"), ROUND_HALF_UP)


def money(value):
    """12500.5 -> «12 500,50»"""
    return f"{round_money(value):,.2f}".replace(",", " ").replace(".", ",")


def read_text(path):
    """UTF-8 (с меткой BOM или без), а если не вышло — cp1251, в ней сохраняет русский Excel."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp1251")


def read_csv(path):
    """Строки с одинаковым invoice_id собираются в один чек с несколькими позициями."""
    text = read_text(path)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(text, newline=""), dialect=dialect))

    if not rows:
        raise ValueError(f"Файл {path.name} пустой")

    invoices = OrderedDict()
    for row in rows:
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        if "invoice_id" not in row:
            raise ValueError(f"В файле {path.name} нет колонки invoice_id")
        inv_id = row["invoice_id"]
        if not inv_id:
            continue
        invoice = invoices.setdefault(
            inv_id, {k: v for k, v in row.items() if k not in ITEM_FIELDS} | {"items": []}
        )
        if row.get("product"):
            invoice["items"].append({f: row.get(f, "") for f in ITEM_FIELDS})
    return list(invoices.values())


def read_json(path):
    data = json.loads(read_text(path))
    if isinstance(data, dict):
        for key in ("invoices", "records"):
            if key in data:
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise ValueError(f"В {path.name} ждём список записей вида [{{...}}, {{...}}]")
    for i, record in enumerate(data, 1):
        record.setdefault("invoice_id", str(i))
        record["invoice_id"] = str(record["invoice_id"])
        items = record.get("items", [])
        if not isinstance(items, list) or not all(isinstance(it, dict) for it in items):
            raise ValueError(f"Чек {record['invoice_id']}: items должен быть списком позиций {{...}}")
    return data


def load_records(path):
    return read_csv(path) if path.suffix.lower() == ".csv" else read_json(path)


def prepare(record):
    """Считает суммы в программе, а не в шаблоне: так итог всегда сходится до копейки."""
    record = dict(record)
    items = record.get("items") or []
    total = Decimal("0")
    prepared = []
    for n, item in enumerate(items, 1):
        qty = to_number(item.get("qty", ""), "qty", record["invoice_id"], default=1)
        price = to_number(item.get("price", ""), "price", record["invoice_id"])
        line_sum = round_money(qty * price)  # итог складываем из уже округлённых строк
        total += line_sum
        prepared.append(item | {
            "n": n,
            "qty": f"{qty.normalize():f}",
            "price": money(price),
            "sum": money(line_sum),
        })
    record["items"] = prepared
    record["total"] = money(total)
    if "amount" in record:
        record["amount"] = money(to_number(record["amount"], "amount", record["invoice_id"]))
    return record


def safe_filename(text):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text).strip("_") or "document"


JINJA = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR, encoding="utf-8"),
    autoescape=True,
    undefined=StrictUndefined,  # пропущенное поле = понятная ошибка, а не пустое место в чеке
)


def missing_field(template_path, record):
    """Пробует заполнить шаблон записью. Вернёт имя поля, которого не хватает, или None."""
    try:
        JINJA.get_template(template_path.name).render(**prepare(record))
    except UndefinedError as e:
        found = re.search(r"'(\w+)' is undefined", str(e))
        return found.group(1) if found else str(e)
    except ValueError:
        # кривое число покажем при сборке, а поля проверим на записи без сумм
        bare = {k: v for k, v in record.items() if k not in ("items", "amount")}
        bare.setdefault("amount", "0")
        return missing_field(template_path, bare) if bare != record else None
    except Exception as e:  # испорченный шаблон не должен ломать приём всех таблиц
        return f"шаблон с ошибкой: {e}"
    return None


def fitting_templates(records):
    """Шаблоны, которые подходят к данным (проверка по первой записи)."""
    templates = list_files(TEMPLATES_DIR, {".html", ".htm"})
    return [t for t in templates if missing_field(t, records[0]) is None]


def quiet_gtk_warnings():
    """GTK на Windows печатает в консоль служебные GLib-GIO-WARNING про приложения Магазина.
    Они безвредные, но пугают. Шум GTK отправляем в никуда, ошибки Python оставляем на экране."""
    if not sys.platform.startswith("win") or sys.stderr is None or not sys.stderr.isatty():
        return
    python_err = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    os.close(devnull)
    sys.stderr = open(python_err, "w", encoding=sys.stderr.encoding, errors="replace", buffering=1)


def render_pdf(template_path, record):
    from weasyprint import CSS, HTML  # импорт здесь: без GTK меню всё равно запустится
    from weasyprint.text.fonts import FontConfiguration

    html = JINJA.get_template(template_path.name).render(**prepare(record))

    name = f"{template_path.stem}_{safe_filename(record['invoice_id'])}.pdf"
    out_path = OUTPUT_DIR / name
    font_config = FontConfiguration()  # без него WeasyPrint не видит шрифты из @font-face
    HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(
        out_path, stylesheets=[CSS(string=FONT_CSS, font_config=font_config)],
        font_config=font_config,
    )
    return out_path


def open_pdf(path):
    """Открывает файл или папку. Вернёт текст ошибки, если не вышло, иначе None."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError as e:
        return f"Не получилось открыть сам: {e}. Файлы лежат тут: {path}"
    return None


def choose(title, options, allow_all=False):
    print(f"\n{title}")
    for i, option in enumerate(options, 1):
        print(f"  {i}. {option}")
    if allow_all:
        print("  0. все сразу")
    while True:
        answer = input("Введите номер (q — выход): ").strip().lower()
        if answer in ("q", "й"):
            sys.exit(0)
        if allow_all and answer == "0":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        print("  Нет такого номера, попробуйте ещё раз.")


def describe(record):
    who = record.get("customer_name") or record.get("recipient") or ""
    date = record.get("date", "")
    # знак «№» шрифт консоли Windows рисует закорючкой, поэтому пишем словом
    return ", ".join(x for x in (f"номер {record['invoice_id']}", who, date) if x)


def run():
    init_dirs()
    data_files = list_files(DATA_DIR, {".csv", ".json"})
    templates = list_files(TEMPLATES_DIR, {".html", ".htm"})
    if not data_files:
        print("В папке data нет файлов CSV или JSON.")
        return
    if not templates:
        print("В папке templates нет HTML-шаблонов.")
        return

    print("=" * 50)
    print("  PDF-чек-мейкер")
    print("=" * 50)

    data_path = data_files[choose("Файлы с данными:", [p.name for p in data_files])]

    try:
        records = load_records(data_path)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"\nНе получилось прочитать {data_path.name}: {e}")
        return
    if not records:
        print(f"\nВ {data_path.name} нет ни одной записи с номером invoice_id.")
        return

    # показываем только шаблоны, которые подходят к выбранным данным
    fitting = fitting_templates(records)
    if not fitting:
        print(f"\nНи один шаблон не подходит к {data_path.name}:")
        for t in templates:
            print(f"  {t.name} ждёт поле «{missing_field(t, records[0])}»")
        return
    if len(fitting) == 1:
        template_path = fitting[0]
        print(f"\nШаблон: {template_path.name} (к этим данным подходит только он)")
    else:
        template_path = fitting[choose("Шаблоны:", [t.name for t in fitting])]

    pick = choose(f"Записи в {data_path.name}:", [describe(r) for r in records], allow_all=True)
    selected = records if pick is None else [records[pick]]

    print()
    made = []
    for record in selected:
        try:
            path = render_pdf(template_path, record)
        except PermissionError:
            print(f"  Файл номер {record['invoice_id']} открыт в другой программе. "
                  "Закройте его и запустите ещё раз.")
            continue
        except OSError as e:
            if "gobject" in str(e).lower() or "pango" in str(e).lower():
                print("WeasyPrint не нашёл GTK/Pango. Windows: установите GTK3 Runtime и перезапустите "
                      "терминал. macOS: brew install pango. Подробнее: "
                      "https://doc.courtbouillon.org/weasyprint/stable/first_steps.html")
                return
            raise
        except UndefinedError as e:
            print(f"  Пропускаю номер {record['invoice_id']}: в записи не хватает поля ({e})")
            continue
        except (ValueError, TemplateError) as e:
            print(f"  Пропускаю номер {record['invoice_id']}: {e}")
            continue
        print(f"  Готово: output/{path.name}")
        made.append(path)

    if made:
        # много файлов: открываем папку, а не десяток окон
        problem = open_pdf(made[0] if len(made) == 1 else OUTPUT_DIR)
        if problem:
            print(f"  {problem}")


if __name__ == "__main__":
    quiet_gtk_warnings()
    try:
        run()
    except (KeyboardInterrupt, EOFError):
        print("\nВыход.")
        sys.exit(0)
    if sys.stdin.isatty():
        input("\nEnter — закрыть окно")  # при запуске двойным щелчком окно не исчезнет сразу
