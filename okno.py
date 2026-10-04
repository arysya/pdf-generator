"""Окно PDF-чек-мейкера: перетащите таблицу, нажмите «Собрать PDF», получите готовые файлы.

Запуск: двойной щелчок по zapusk.bat или  python okno.py
Вся работа с данными и PDF живёт в pdf_generator.py, здесь только окно.
"""

import ctypes
import queue
import re
import sys
import threading
import tkinter as tk
import tkinter.font
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pdf_generator as core

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # без библиотеки окно работает, просто только через кнопку
    TkinterDnD = None

# Цвета те же, что в шаблонах чека и сертификата
BG = "#f6f8f7"
CARD = "#ffffff"
ZONE = "#eef3f1"
ZONE_HOVER = "#dfe9e5"
ACCENT = "#3e5f54"
ACCENT_HOVER = "#2f4a41"
LINE = "#9fb5ad"
TEXT = "#23302c"
MUTED = "#6b7a75"
ERROR = "#a33a3a"
OK = "#2f6b4f"

SANS = "Golos Text"
FALLBACK = "Segoe UI"


def load_fonts():
    """Шрифт Golos из папки fonts видит только эта программа, в систему он не ставится."""
    if sys.platform != "win32":
        return
    for path in core.FONTS_DIR.glob("*.ttf"):
        ctypes.windll.gdi32.AddFontResourceExW(str(path), 0x10, 0)  # 0x10 = только для нас
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # не размывать окно при масштабе 125–150%
    except (AttributeError, OSError):
        pass  # старая Windows: окно будет чуть мягче, но рабочее


def template_title(path):
    """«Чек № {{ invoice_id }}» в <title> шаблона превращается в понятное «Чек»."""
    text = path.read_text(encoding="utf-8", errors="replace")
    found = re.search(r"<title>(.*?)</title>", text, re.S)
    if not found:
        return path.stem
    title = re.sub(r"\{\{.*?\}\}|№", "", found.group(1)).strip(" ,-")
    return title or path.stem


def count_word(n):
    """1 документ, 3 документа, 7 документов."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} документ"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} документа"
    return f"{n} документов"


class App:
    def __init__(self, root):
        self.root = root
        self.records = []
        self.data_path = None
        self.templates = []
        self.busy = False
        self.events = queue.Queue()

        families = set(tkinter.font.families(root))
        family = SANS if SANS in families else FALLBACK
        self.f_title = (family, 20, "bold")
        self.f_text = (family, 11)
        self.f_small = (family, 10)
        self.f_zone = (family, 13)
        self.f_button = (family, 12, "bold")

        # на экране с увеличением 125–150% всё в окне крупнее, поэтому размеры умножаем
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96)
        self.wrap_px = int(540 * self.scale)
        root.title("PDF-чек-мейкер")
        root.configure(bg=BG)
        root.geometry(f"{int(640 * self.scale)}x{int(470 * self.scale)}")
        root.minsize(int(560 * self.scale), int(450 * self.scale))
        self.build()

    # ---------- внешний вид ----------

    def build(self):
        wrap = tk.Frame(self.root, bg=BG, padx=28, pady=24)
        wrap.pack(fill="both", expand=True)

        tk.Label(wrap, text="PDF-чек-мейкер", font=self.f_title, fg=TEXT, bg=BG).pack(anchor="w")
        tk.Label(
            wrap, text="Перетащите таблицу, и программа соберёт по ней готовые PDF",
            font=self.f_text, fg=MUTED, bg=BG, wraplength=self.wrap_px, justify="left",
        ).pack(anchor="w", pady=(2, 18))

        # Область для перетаскивания: пунктирная рамка на холсте
        self.zone = tk.Canvas(wrap, height=int(170 * self.scale), bg=ZONE, highlightthickness=0, cursor="hand2")
        self.zone.pack(fill="x")
        self.zone.bind("<Configure>", lambda e: self.draw_zone())
        self.zone.bind("<Button-1>", lambda e: self.pick_file())
        self.zone_title = "Перетащите сюда таблицу"
        self.zone_hint = "CSV или JSON. Или нажмите, чтобы выбрать файл"
        self.zone_color = MUTED

        if TkinterDnD is not None:
            self.zone.drop_target_register(DND_FILES)
            self.zone.dnd_bind("<<DropEnter>>", lambda e: self.hover(True))
            self.zone.dnd_bind("<<DropLeave>>", lambda e: self.hover(False))
            self.zone.dnd_bind("<<Drop>>", self.on_drop)

        # Строка выбора шаблона: появляется, только если подходят несколько
        self.tpl_row = tk.Frame(wrap, bg=BG)
        tk.Label(self.tpl_row, text="Вид документа:", font=self.f_text, fg=TEXT, bg=BG).pack(side="left")
        self.tpl_var = tk.StringVar()
        self.tpl_box = ttk.Combobox(self.tpl_row, textvariable=self.tpl_var, state="readonly",
                                    font=self.f_text, width=28)
        self.tpl_box.pack(side="left", padx=(10, 0))

        self.info = tk.Label(wrap, text="", font=self.f_text, fg=TEXT, bg=BG, justify="left",
                             anchor="w", wraplength=self.wrap_px)
        self.info.pack(fill="x", pady=(16, 0))

        bottom = tk.Frame(wrap, bg=BG)
        bottom.pack(fill="x", side="bottom")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Green.Horizontal.TProgressbar", troughcolor=ZONE, background=ACCENT,
                        bordercolor=ZONE, lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)
        self.progress = ttk.Progressbar(bottom, style="Green.Horizontal.TProgressbar", mode="determinate")
        self.progress.pack(fill="x", pady=(0, 10))

        self.status = tk.Label(bottom, text="", font=self.f_small, fg=MUTED, bg=BG, anchor="w",
                               justify="left", wraplength=self.wrap_px)
        self.status.pack(fill="x", pady=(0, 12))

        buttons = tk.Frame(bottom, bg=BG)
        buttons.pack(fill="x")
        self.go = tk.Button(
            buttons, text="Собрать PDF", font=self.f_button, fg="white", bg=ACCENT,
            activebackground=ACCENT_HOVER, activeforeground="white", relief="flat", bd=0,
            padx=26, pady=10, cursor="hand2", command=self.start, state="disabled",
            disabledforeground="white",
        )
        self.go.pack(side="left")
        self.set_go(False)
        self.folder_btn = tk.Button(
            buttons, text="Открыть папку с PDF", font=self.f_text, fg=ACCENT, bg=BG,
            activebackground=ZONE, activeforeground=ACCENT, relief="flat", bd=0,
            padx=14, pady=10, cursor="hand2", command=self.open_output,
        )
        self.folder_btn.pack(side="left", padx=(12, 0))

    def draw_zone(self):
        c = self.zone
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        c.create_rectangle(6, 6, w - 6, h - 6, outline=LINE, width=2, dash=(8, 6))
        c.create_text(w / 2, h / 2 - 4, text=self.zone_title, font=self.f_zone, fill=TEXT,
                      anchor="s", width=w - 60, justify="center")
        c.create_text(w / 2, h / 2 + 6, text=self.zone_hint, font=self.f_small, fill=self.zone_color,
                      anchor="n", width=w - 60, justify="center")

    def set_go(self, enabled, text="Собрать PDF"):
        """Пока нет файла, кнопка бледная: видно, что нажимать её ещё рано."""
        self.go.configure(state="normal" if enabled else "disabled", text=text,
                          bg=ACCENT if enabled else "#b9c7c2", cursor="hand2" if enabled else "arrow")

    def hover(self, on):
        self.zone.configure(bg=ZONE_HOVER if on else ZONE)
        return "copy"

    def say(self, text, color=MUTED):
        self.status.configure(text=text, fg=color)

    # ---------- выбор файла ----------

    def pick_file(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(
            title="Выберите таблицу",
            initialdir=core.DATA_DIR,
            filetypes=[("Таблицы", "*.csv *.json"), ("Все файлы", "*.*")],
        )
        if path:
            self.load(Path(path))

    def on_drop(self, event):
        self.hover(False)
        if self.busy:
            return "copy"
        files = self.root.tk.splitlist(event.data)  # путь с пробелами приходит в {фигурных скобках}
        if files:
            self.load(Path(files[0]))
            if len(files) > 1 and self.records:
                self.say(f"Взяла только {Path(files[0]).name}: перетаскивайте по одной таблице. "
                         "Нажмите «Собрать PDF».")
        return "copy"

    def load(self, path):
        self.progress["value"] = 0
        self.tpl_row.pack_forget()
        self.set_go(False)
        self.records, self.templates = [], []

        if path.suffix.lower() not in (".csv", ".json"):
            return self.reject(path, "Это не таблица. Нужен файл CSV или JSON (в Excel: «Сохранить как» → CSV).")
        try:
            records = core.load_records(path)
            if not records:
                return self.reject(path, "В таблице нет ни одной строки с номером invoice_id.")
            templates = core.fitting_templates(records)
        except Exception as e:  # окно запущено без консоли: любую ошибку показываем в окне
            return self.reject(path, f"Не получилось прочитать файл: {e}")
        if not templates:
            return self.reject(path, "Ни один образец документа не подходит к этой таблице. "
                                     "Проверьте названия колонок.")

        self.data_path, self.records, self.templates = path, records, templates
        self.zone_title = path.name
        self.zone_hint = "Файл принят. Чтобы заменить, перетащите другой"
        self.zone_color = OK
        self.draw_zone()

        names = [template_title(t) for t in templates]
        if len(templates) > 1:
            self.tpl_box.configure(values=names)
            self.tpl_box.current(0)
            self.tpl_row.pack(anchor="w", pady=(16, 0), before=self.info)
        if len(templates) == 1:
            text = f"В таблице {count_word(len(records))}, вид: {names[0].lower()}."
        else:
            text = f"В таблице {count_word(len(records))}. Выберите, какой документ собрать."
        self.info.configure(text=text)
        self.say("Нажмите «Собрать PDF».")
        self.set_go(True)

    def reject(self, path, why):
        self.zone_title = path.name
        self.zone_hint = "Перетащите другой файл"
        self.zone_color = ERROR
        self.draw_zone()
        self.info.configure(text="")
        self.say(why, ERROR)

    def open_output(self, target=None):
        problem = core.open_pdf(target or core.OUTPUT_DIR)
        if problem:
            self.say(problem, ERROR)

    # ---------- сборка ----------

    def start(self):
        if self.busy or not self.records:
            return
        template = self.templates[self.tpl_box.current()] if len(self.templates) > 1 else self.templates[0]
        self.busy = True
        self.set_go(False, "Собираю…")
        self.progress.configure(maximum=len(self.records), value=0)
        self.worker = threading.Thread(target=self.work, args=(template, list(self.records)), daemon=True)
        self.worker.start()
        self.root.after(100, self.poll)

    def work(self, template, records):
        """Работает в фоне, чтобы окно не замирало. С окном говорит только через очередь."""
        made, problems = [], []
        try:
            for i, record in enumerate(records, 1):
                num = record["invoice_id"]
                try:
                    made.append(core.render_pdf(template, record))
                except PermissionError:
                    problems.append(f"номер {num}: файл открыт в другой программе, закройте его")
                except OSError as e:
                    if "gobject" in str(e).lower() or "pango" in str(e).lower():
                        problems = ["Не найден GTK. Установите GTK3 Runtime и запустите программу снова."]
                        return
                    problems.append(f"номер {num}: {e}")
                except core.UndefinedError as e:
                    found = re.search(r"'(\w+)' is undefined", str(e))
                    problems.append(f"номер {num}: не хватает колонки {found.group(1) if found else e}")
                except (ValueError, core.TemplateError) as e:
                    problems.append(f"номер {num}: {e}")
                except Exception as e:  # без консоли иначе ошибка пропадёт молча
                    problems.append(f"номер {num}: непредвиденная ошибка {type(e).__name__}: {e}")
                self.events.put(("step", i, len(records)))
        finally:
            self.events.put(("done", made, problems))  # кнопка оживёт при любом исходе

    def poll(self):
        """Окно раз в 100 мс забирает из очереди, что успел сделать фоновый поток."""
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "step":
                    self.progress["value"] = event[1]
                    self.say(f"Собираю {event[1]} из {event[2]}…")
                else:
                    return self.finish(event[1], event[2])
        except queue.Empty:
            self.root.after(100, self.poll)

    def finish(self, made, problems):
        self.busy = False
        self.set_go(True)
        if made:
            text = f"Готово: {count_word(len(made))} в папке output."
            if problems:
                text += f" Не собралось: {len(problems)}. " + "; ".join(problems[:3])
            self.say(text, OK if not problems else ERROR)
            self.open_output(made[0] if len(made) == 1 else None)
        else:
            self.say("Ничего не собралось. " + "; ".join(problems[:3]), ERROR)

    def on_close(self):
        if self.busy and not messagebox.askyesno(
            "Идёт сборка", "PDF ещё собираются. Закрыть окно? Последний файл может остаться недописанным."
        ):
            return
        self.root.destroy()


def main():
    load_fonts()
    core.init_dirs()
    root = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
