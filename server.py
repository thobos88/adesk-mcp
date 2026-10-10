#!/usr/bin/env python3
"""MCP-сервер для Adesk — только чтение.

Транспорт: stdio, JSON-RPC 2.0, одно сообщение на строку.
Зависимости: только стандартная библиотека Python 3.10+.

Ключ API берётся только из окружения:
  ADESK_API_TOKEN — ключ Adesk;
  ADESK_ENV_FILE  — путь к .env-файлу, откуда взять ADESK_API_TOKEN, если переменной нет.

К Adesk уходят только GET-запросы к четырём разрешённым адресам. Любой другой
метод или адрес отклоняется до сетевого запроса. Ключ не выводится ни в ответы,
ни в журнал.
"""
from __future__ import annotations

import json
import os
import resource
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, namedtuple
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

SERVER_NAME = "adesk-mcp"
SERVER_VERSION = "0.1.0"
# Первая версия — самая новая: её отдаём, если клиент просит неизвестную.
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

API_BASE = "https://api.adesk.ru/v1/"
ALLOWED_METHOD = "GET"
ALLOWED_PATHS = frozenset({"bank-accounts", "projects", "transactions", "transactions/categories"})

MSK = timezone(timedelta(hours=3))  # Россия без перехода на летнее время
WINDOW_DAYS = 4                     # окна по 4 дня: постраничная выдача Adesk иногда обрывается
PAGE = 100
MAX_PERIOD_DAYS = 366
HTTP_TIMEOUT = 60

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603


class ConfigError(Exception):
    """Нет ключа или файла окружения. Сообщение безопасно показывать."""


class ReadOnlyViolation(Exception):
    """Попытка не-GET запроса или запроса к неразрешённому адресу."""


class AdeskError(Exception):
    """Ошибка чтения Adesk. Сообщение не содержит адрес и ключ."""


class ToolInputError(Exception):
    """Неверные аргументы инструмента. Сообщение безопасно показывать."""


# ---------------------------------------------------------------- конфигурация

def parse_env_file(path):
    values = {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        raise ConfigError("Файл из ADESK_ENV_FILE не читается") from None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        try:
            parsed = shlex.split(value, comments=True)
        except ValueError:
            parsed = [value.strip()]
        values[key.strip()] = " ".join(parsed)
    return values


def load_token(environ=None):
    environ = os.environ if environ is None else environ
    token = (environ.get("ADESK_API_TOKEN") or "").strip()
    if token:
        return token
    path = (environ.get("ADESK_ENV_FILE") or "").strip()
    if path:
        token = (parse_env_file(path).get("ADESK_API_TOKEN") or "").strip()
        if token:
            return token
        raise ConfigError("В файле из ADESK_ENV_FILE нет ADESK_API_TOKEN")
    raise ConfigError("Не задан ключ: укажите ADESK_API_TOKEN или ADESK_ENV_FILE")


# ---------------------------------------------------------------- клиент Adesk

class AdeskClient:
    """Клиент только для чтения. Единственная публичная операция — get()."""

    def __init__(self, token, urlopen=None, sleep=time.sleep, timeout=HTTP_TIMEOUT):
        if not token:
            raise ConfigError("Пустой ключ Adesk")
        self._token = token
        self._urlopen = urlopen or urllib.request.urlopen
        self._sleep = sleep
        self._timeout = timeout

    def _safe(self, text):
        return str(text).replace(self._token, "***")

    def _request(self, method, path, params):
        # Запрет записи: проверка до формирования запроса и до сети.
        if method != ALLOWED_METHOD:
            raise ReadOnlyViolation(f"Запрещено: метод {method}. Сервер только читает Adesk (GET).")
        if path not in ALLOWED_PATHS:
            raise ReadOnlyViolation("Запрещено: адрес Adesk вне списка разрешённых для чтения")
        query = dict(params)
        query["api_token"] = self._token
        req = urllib.request.Request(API_BASE + path + "?" + urllib.parse.urlencode(query), method=ALLOWED_METHOD)
        if req.get_method() != ALLOWED_METHOD or req.data is not None:
            raise ReadOnlyViolation("Запрещено: запрос не является чистым GET")
        last = "нет ответа"
        for attempt in range(3):
            try:
                with self._urlopen(req, timeout=self._timeout) as response:
                    data = json.load(response)
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                e.close()
                if e.code < 500 and e.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last = type(e).__name__
            else:
                if not isinstance(data, dict):
                    raise AdeskError("Adesk вернул ответ неожиданной формы")
                if not data.get("success"):
                    detail = data.get("errors") or data.get("message") or ""
                    raise AdeskError(self._safe("Adesk отклонил запрос: " + json.dumps(detail, ensure_ascii=False)[:300]))
                return data
            if attempt < 2:
                self._sleep(1 + attempt)
        raise AdeskError(self._safe(f"Adesk недоступен ({last}); адрес запроса скрыт"))

    def get(self, path, **params):
        return self._request(ALLOWED_METHOD, path, params)


# ---------------------------------------------------------------- данные

Part = namedtuple("Part", "op_id sign pay accrual amount cat_id project_id project contractor description account transfer planned")
Category = namedtuple("Category", "id name type kind archived")


def money(value):
    if value in (None, ""):
        return Decimal(0)
    try:
        out = Decimal(str(value).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        raise AdeskError("Некорректная сумма в ответе Adesk") from None
    if not out.is_finite():
        raise AdeskError("Некорректная сумма в ответе Adesk")
    return out


def iso_day(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise AdeskError("Некорректная дата в ответе Adesk") from None


def _name(obj):
    return (obj or {}).get("name") or "" if isinstance(obj, dict) else (obj or "")


def parse_operation(t):
    """Одна операция Adesk → список частей. Разбитая операция → её части (parts)."""
    op_type = int(t.get("type") or 0)
    if op_type not in (1, 2):
        raise AdeskError("Неизвестный тип операции Adesk")
    sign = 1 if op_type == 1 else -1
    parts = t.get("parts") or []
    if t.get("isSplitted") and not parts:
        raise AdeskError("У разбитой операции Adesk нет частей")
    if parts and abs(sum(money(p.get("amount")) for p in parts) - money(t.get("amount"))) > Decimal("0.01"):
        raise AdeskError("Сумма частей операции Adesk не совпала с операцией")
    out = []
    for p in parts or [t]:
        cat = p.get("category")
        project = p.get("project") or None
        out.append(Part(
            op_id=t.get("id"), sign=sign, pay=iso_day(t.get("dateIso") or t.get("date")),
            accrual=iso_day(p.get("relatedDateIso") or t.get("relatedDateIso")),
            amount=money(p.get("amount")),
            cat_id=cat.get("id") if isinstance(cat, dict) else None,
            project_id=project.get("id") if isinstance(project, dict) else None,
            project=_name(project), contractor=_name(p.get("contractor") or t.get("contractor")),
            description=(t.get("description") or "").strip(), account=_name(t.get("bankAccount")),
            transfer=bool(t.get("isTransfer")), planned=bool(t.get("isPlanned"))))
    return out


def complete_window(fetch, start, end):
    """Все операции окна [start, end] с проверкой по recordsFiltered.

    fetch(start, end, offset) → (rows, records_filtered).
    Обрыв выдачи → окно делится пополам; в окне из одного дня — дочитываем страницами.
    """
    rows, expected = fetch(start, end, 0)
    if len(rows) == expected and len({r.get("id") for r in rows}) == expected:
        return rows
    if start < end:
        mid = start + (end - start) // 2
        return complete_window(fetch, start, mid) + complete_window(fetch, mid + timedelta(days=1), end)
    while len(rows) < expected:
        tail, again = fetch(start, end, len(rows))
        if again != expected or not tail:
            raise AdeskError("Adesk отдал неполный список операций за день; повторите позже")
        if {r.get("id") for r in rows} & {r.get("id") for r in tail}:
            raise AdeskError("Adesk повторил страницу операций; повторите позже")
        rows += tail
    if len(rows) != expected or len({r.get("id") for r in rows}) != expected:
        raise AdeskError("Количество операций Adesk не совпало с recordsFiltered")
    return rows


class Adesk:
    """Чтение и разбор данных Adesk поверх AdeskClient."""

    def __init__(self, client):
        self.client = client

    def categories(self):
        data = self.client.get("transactions/categories")
        cats = {c["id"]: Category(c["id"], c.get("name") or "", int(c.get("type") or 0), int(c.get("kind") or 0), bool(c.get("isArchived")))
                for c in data.get("categories") or []}
        if len(cats) != int(data.get("recordsFiltered", len(cats))):
            raise AdeskError("Adesk отдал неполный справочник статей")
        return cats

    def projects(self):
        for attempt in range(3):
            data = self.client.get("projects")  # один запрос без страниц — справочник целиком
            rows = data.get("projects") or []
            ids = {p.get("id") for p in rows}
            if len(rows) == len(ids) == int(data.get("recordsFiltered", len(rows))):
                return [dict(id=p.get("id"), name=p.get("name") or "", category=_name(p.get("category")),
                             finished=bool(p.get("isFinished")), archived=bool(p.get("isArchived"))) for p in rows]
        raise AdeskError("Adesk отдал неполный справочник проектов; повторите позже")

    def bank_accounts(self):
        data = self.client.get("bank-accounts")
        return [dict(name=a.get("name") or "", bank=a.get("bankName") or "", legal=_name(a.get("legalEntity")),
                     balance=money(a.get("amount")), currency=a.get("currency") or "", status=a.get("status") or "",
                     excluded=bool(a.get("isExcludedFromBusinessMoney"))) for a in data.get("bankAccounts") or []]

    def operations(self, start, end, basis="payment"):
        """Части операций за период. basis: payment — по дате платежа, accrual — по дате начисления."""
        extra = {"date_type": "accrual"} if basis == "accrual" else {}

        def fetch(s, e, offset):
            data = self.client.get("transactions", range="custom", range_start=s.isoformat(), range_end=e.isoformat(),
                                   length=PAGE, start=offset, **extra)
            return data.get("transactions") or [], int(data.get("recordsFiltered", 0))

        parts, seen, s = [], set(), start
        while s <= end:
            e = min(end, s + timedelta(days=WINDOW_DAYS - 1))
            for t in complete_window(fetch, s, e):  # разбираем сразу: сырой JSON не копится
                if t.get("id") in seen:
                    continue
                seen.add(t.get("id"))
                parts.extend(parse_operation(t))
            s = e + timedelta(days=1)
        return parts

    def project_operations(self, project_id):
        for attempt in range(3):
            rows, expected = [], None
            while True:
                data = self.client.get("transactions", project=project_id, length=PAGE, start=len(rows))
                expected = int(data.get("recordsFiltered", 0))
                tail = data.get("transactions") or []
                rows += tail
                if len(rows) >= expected or not tail:
                    break
            if len(rows) == len({r.get("id") for r in rows}) == expected:
                return [p for t in rows for p in parse_operation(t)]
        raise AdeskError("Adesk отдал неполный список операций проекта; повторите позже")


# ---------------------------------------------------------------- расчёты

def month_bounds(month):
    try:
        y, m = map(int, month.split("-"))
        first = date(y, m, 1)
    except (ValueError, AttributeError):
        raise ToolInputError("Месяц укажите в виде ГГГГ-ММ, например 2026-08") from None
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return first, nxt - timedelta(days=1), nxt


def month_is_final(month, today):
    """Месяц окончателен с 11-го числа следующего месяца (зарплата начисляется задним числом до 10-го)."""
    _, _, nxt = month_bounds(month)
    final_from = nxt.replace(day=11)
    return today >= final_from, final_from


def pnl(parts, categories, month):
    """ОПиУ за месяц по дате начисления.

    В расчёт идут только статьи операционной деятельности (kind=1).
    Переводы между счетами, плановые операции, статьи kind 2/3 (инвестиционные,
    финансовые: кредиты, дивиденды и т. п.) и операции без статьи — не входят.
    """
    income, expense, other = defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)
    stats = dict(transfers=0, planned=0, uncategorized=0, uncategorized_sum=Decimal(0))
    for p in parts:
        if p.accrual is None or p.accrual.strftime("%Y-%m") != month:
            continue
        if p.transfer:
            stats["transfers"] += 1
            continue
        if p.planned:
            stats["planned"] += 1
            continue
        cat = categories.get(p.cat_id)
        if cat is None:
            stats["uncategorized"] += 1
            stats["uncategorized_sum"] += p.amount * p.sign
            continue
        if cat.kind != 1:
            other[cat.name] += p.amount * p.sign
            continue
        (income if p.sign > 0 else expense)[cat.name] += p.amount
    total_in, total_out = sum(income.values(), Decimal(0)), sum(expense.values(), Decimal(0))
    return dict(month=month, income=dict(income), expense=dict(expense), other=dict(other),
                income_total=total_in, expense_total=total_out, profit=total_in - total_out, **stats)


def by_category(parts, categories, direction, operating_only=True):
    sign = 1 if direction == "income" else -1
    sums, counts = defaultdict(Decimal), defaultdict(int)
    for p in parts:
        if p.transfer or p.planned or p.sign != sign:
            continue
        cat = categories.get(p.cat_id)
        if operating_only and (cat is None or cat.kind != 1):
            continue
        name = cat.name if cat else "Без статьи"
        sums[name] += p.amount
        counts[name] += 1
    return sums, counts


# ---------------------------------------------------------------- оформление

def rub(value):
    q = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = f"{abs(q):,.2f}".replace(",", " ").replace(".", ",")
    return ("-" if q < 0 else "") + text + " ₽"


def pct(part, whole):
    return str((Decimal(part) / Decimal(whole) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)).replace(".", ",") + " %"


def short(text, limit=70):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def norm(text):
    return str(text).casefold().replace("ё", "е")


def matches(text, query):
    hay = norm(text)
    return all(word in hay for word in norm(query).split())


def today_msk():
    return datetime.now(MSK).date()


# ---------------------------------------------------------------- аргументы

def arg_str(args, key, default=None, required=False):
    value = args.get(key, default)
    if value is None or value == "":
        if required:
            raise ToolInputError(f"Не задан параметр {key}")
        return default
    if not isinstance(value, str):
        raise ToolInputError(f"Параметр {key} должен быть строкой")
    return value.strip()


def arg_int(args, key, default, low, high):
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ToolInputError(f"Параметр {key} должен быть целым числом")
    if not low <= int(value) <= high:
        raise ToolInputError(f"Параметр {key} должен быть от {low} до {high}")
    return int(value)


def arg_bool(args, key, default=False):
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise ToolInputError(f"Параметр {key} должен быть true или false")
    return value


def arg_enum(args, key, choices, default):
    value = args.get(key, default)
    if value not in choices:
        raise ToolInputError(f"Параметр {key}: допустимо {', '.join(choices)}")
    return value


def arg_period(args):
    try:
        start = date.fromisoformat(arg_str(args, "date_from", required=True))
        end = date.fromisoformat(arg_str(args, "date_to", required=True))
    except ValueError:
        raise ToolInputError("Даты укажите в виде ГГГГ-ММ-ДД") from None
    if start > end:
        raise ToolInputError("date_from позже date_to")
    if (end - start).days + 1 > MAX_PERIOD_DAYS:
        raise ToolInputError(f"Период не длиннее {MAX_PERIOD_DAYS} дней")
    return start, end


BASIS_TEXT = {"payment": "по дате платежа", "accrual": "по дате начисления"}

# ---------------------------------------------------------------- инструменты

def tool_accounts(adesk, args):
    include_closed = arg_bool(args, "include_closed", False)
    accounts = [a for a in adesk.bank_accounts() if include_closed or a["status"] != "closed"]
    accounts.sort(key=lambda a: (a["legal"], -a["balance"]))
    lines = [f"Счета Adesk: {len(accounts)}{' (вместе с закрытыми)' if include_closed else ' открытых'}. Остаток на сейчас."]
    by_legal = defaultdict(Decimal)
    for a in accounts:
        flags = [x for x, on in (("закрыт", a["status"] == "closed"), ("вне денег бизнеса", a["excluded"])) if on]
        cur = "" if a["currency"] in ("RUR", "RUB", "") else f" [{a['currency']}]"
        lines.append(f"- {a['name']}{' · ' + a['bank'] if a['bank'] else ''}{' · ' + a['legal'] if a['legal'] else ''}: "
                     f"{rub(a['balance'])}{cur}{' (' + ', '.join(flags) + ')' if flags else ''}")
        by_legal[a["legal"] or "без юрлица"] += a["balance"]
    if len(by_legal) > 1:
        lines.append("По юрлицам: " + "; ".join(f"{k} — {rub(v)}" for k, v in sorted(by_legal.items())))
    lines.append(f"Итого: {rub(sum(by_legal.values(), Decimal(0)))}")
    return "\n".join(lines)


def tool_projects(adesk, args):
    query = arg_str(args, "query", "")
    category = arg_str(args, "category", "")
    include_archived = arg_bool(args, "include_archived", False)
    limit = arg_int(args, "limit", 30, 1, 200)
    rows = [p for p in adesk.projects()
            if (include_archived or not p["archived"])
            and (not query or matches(p["name"], query))
            and (not category or matches(p["category"], category))]
    rows.sort(key=lambda p: -int(p["id"] or 0))
    lines = [f"Проектов найдено: {len(rows)}" + (f", показаны первые {limit} (сначала новые)" if len(rows) > limit else "")]
    for p in rows[:limit]:
        flags = [x for x, on in (("завершён", p["finished"]), ("в архиве", p["archived"])) if on]
        lines.append(f"- id {p['id']}: {p['name']}" + (f" · {p['category']}" if p["category"] else "")
                     + (f" ({', '.join(flags)})" if flags else ""))
    if not rows:
        lines.append("Совпадений нет. Попробуйте часть адреса или номера договора.")
    return "\n".join(lines)


def tool_project_money(adesk, args):
    project_id = args.get("project_id")
    query = arg_str(args, "query", "")
    if project_id is not None:
        project_id = arg_int(args, "project_id", None, 1, 10**12)
    elif not query:
        raise ToolInputError("Укажите project_id или query")
    projects = adesk.projects()
    if project_id is None:
        found = [p for p in projects if matches(p["name"], query)]
        if len(found) != 1:
            head = f"По запросу «{query}» найдено проектов: {len(found)}. Уточните запрос или укажите project_id."
            return "\n".join([head] + [f"- id {p['id']}: {p['name']}" for p in found[:20]])
        project = found[0]
    else:
        project = next((p for p in projects if p["id"] == project_id), None)
        if project is None:
            return f"Проект id {project_id} не найден."
    cats = adesk.categories()
    parts = [p for p in adesk.project_operations(project["id"]) if p.project_id == project["id"] and not p.transfer and not p.planned]
    income, expense = defaultdict(Decimal), defaultdict(Decimal)
    for p in parts:
        name = cats[p.cat_id].name if p.cat_id in cats else "Без статьи"
        (income if p.sign > 0 else expense)[name] += p.amount
    tin, tout = sum(income.values(), Decimal(0)), sum(expense.values(), Decimal(0))
    days = [p.pay for p in parts if p.pay]
    lines = [f"Проект id {project['id']}: {project['name']}" + (f" · {project['category']}" if project["category"] else ""),
             f"Операций (частей): {len(parts)}" + (f", платежи с {min(days):%d.%m.%Y} по {max(days):%d.%m.%Y}" if days else ""),
             f"Доходы: {rub(tin)}", f"Расходы: {rub(tout)}", f"Разница (доходы − расходы): {rub(tin - tout)}"]
    if tin:
        lines.append(f"Доля разницы в доходах: {pct(tin - tout, tin)}")
    for title, block in (("Доходы по статьям", income), ("Расходы по статьям", expense)):
        if block:
            lines.append(title + ":")
            lines += [f"- {k}: {rub(v)}" for k, v in sorted(block.items(), key=lambda kv: -kv[1])]
    if arg_bool(args, "include_operations", False):
        lines.append("Операции (сначала новые):")
        for p in sorted(parts, key=lambda p: (p.pay or date.min), reverse=True)[:50]:
            bits = [f"{p.pay:%d.%m.%Y}" if p.pay else "без даты", ("+" if p.sign > 0 else "−") + rub(p.amount),
                    cats[p.cat_id].name if p.cat_id in cats else "Без статьи"]
            if p.contractor:
                bits.append(short(p.contractor, 40))
            lines.append("- " + " · ".join(bits))
    lines.append("Переводы между счетами и плановые операции не учитываются. Данные — по дате платежа за всё время.")
    return "\n".join(lines)


def tool_pnl_month(adesk, args, today=None):
    first, last, _ = month_bounds(arg_str(args, "month", required=True))
    month = first.strftime("%Y-%m")  # «2026-8» → «2026-08»
    today = today or today_msk()
    if first > today:
        raise ToolInputError("Этот месяц ещё не начался")
    cats = adesk.categories()
    r = pnl(adesk.operations(first, last, basis="accrual"), cats, month)
    final, final_from = month_is_final(month, today)
    lines = [f"ОПиУ за {month} по дате начисления (статьи операционной деятельности).",
             ("Месяц окончателен." if final else
              f"ВНИМАНИЕ: месяц ещё не окончателен — зарплату и другие расходы начисляют задним числом; цифры станут окончательными с {final_from:%d.%m.%Y}."),
             f"Выручка (операционные доходы): {rub(r['income_total'])}",
             f"Расходы: {rub(r['expense_total'])}",
             f"Прибыль: {rub(r['profit'])}"]
    if r["income_total"]:
        lines.append(f"Рентабельность: {pct(r['profit'], r['income_total'])}")
    for title, block in (("Доходы по статьям", r["income"]), ("Расходы по статьям", r["expense"])):
        if block:
            lines.append(title + ":")
            lines += [f"- {k}: {rub(v)}" for k, v in sorted(block.items(), key=lambda kv: -kv[1])]
    if r["other"]:
        lines.append("Не входит в ОПиУ (инвестиционные и финансовые статьи, знак: + приход, − расход):")
        lines += [f"- {k}: {rub(v)}" for k, v in sorted(r["other"].items())]
    notes = []
    if r["uncategorized"]:
        notes.append(f"без статьи — {r['uncategorized']} шт. на {rub(r['uncategorized_sum'])} (в прибыль не вошли)")
    if r["planned"]:
        notes.append(f"плановых — {r['planned']} шт. (не вошли)")
    if r["transfers"]:
        notes.append(f"переводов между счетами — {r['transfers']} шт. (не вошли)")
    if notes:
        lines.append("Пропущено: " + "; ".join(notes) + ".")
    lines.append("Закрытые месяцы тоже могут исправляться задним числом.")
    return "\n".join(lines)


def tool_category_totals(adesk, args):
    start, end = arg_period(args)
    direction = arg_enum(args, "direction", ("expense", "income"), "expense")
    basis = arg_enum(args, "basis", ("payment", "accrual"), "payment")
    operating_only = not arg_bool(args, "include_non_operating", False)
    cats = adesk.categories()
    day_of = (lambda p: p.pay) if basis == "payment" else (lambda p: p.accrual)
    parts = [p for p in adesk.operations(start, end, basis) if day_of(p) and start <= day_of(p) <= end]
    sums, counts = by_category(parts, cats, direction, operating_only)
    total = sum(sums.values(), Decimal(0))
    what = "Расходы" if direction == "expense" else "Доходы"
    lines = [f"{what} по статьям {start:%d.%m.%Y}–{end:%d.%m.%Y}, {BASIS_TEXT[basis]}"
             + (", только операционные статьи" if operating_only else ", все статьи") + ".",
             f"Итого: {rub(total)}"]
    for name, value in sorted(sums.items(), key=lambda kv: -kv[1]):
        share = pct(value, total) if total else "—"
        lines.append(f"- {name}: {rub(value)} ({share}, операций: {counts[name]})")
    if not sums:
        lines.append("Операций нет.")
    lines.append("Переводы между счетами и плановые операции не учитываются.")
    return "\n".join(lines)


def tool_operations(adesk, args):
    start, end = arg_period(args)
    basis = arg_enum(args, "basis", ("payment", "accrual"), "payment")
    direction = arg_enum(args, "direction", ("all", "income", "expense"), "all")
    category = arg_str(args, "category", "")
    project = arg_str(args, "project", "")
    contractor = arg_str(args, "contractor", "")
    text = arg_str(args, "text", "")
    include_transfers = arg_bool(args, "include_transfers", False)
    limit = arg_int(args, "limit", 50, 1, 200)
    min_amount = args.get("min_amount", 0)
    if isinstance(min_amount, bool) or not isinstance(min_amount, (int, float)) or min_amount < 0:
        raise ToolInputError("Параметр min_amount должен быть неотрицательным числом")
    cats = adesk.categories()
    rows = []
    for p in adesk.operations(start, end, basis):
        d = p.pay if basis == "payment" else p.accrual
        if not d or not start <= d <= end or (p.transfer and not include_transfers):
            continue
        if direction != "all" and p.sign != (1 if direction == "income" else -1):
            continue
        cat_name = cats[p.cat_id].name if p.cat_id in cats else ("Перевод" if p.transfer else "Без статьи")
        if (category and not matches(cat_name, category)) or (project and not matches(p.project, project)) \
                or (contractor and not matches(p.contractor, contractor)) or (text and not matches(p.description, text)) \
                or p.amount < Decimal(str(min_amount)):
            continue
        rows.append((d, p, cat_name))
    rows.sort(key=lambda r: (r[0], r[1].op_id or 0), reverse=True)
    tin = sum((p.amount for _, p, _ in rows if p.sign > 0 and not p.transfer), Decimal(0))
    tout = sum((p.amount for _, p, _ in rows if p.sign < 0 and not p.transfer), Decimal(0))
    lines = [f"Операции {start:%d.%m.%Y}–{end:%d.%m.%Y}, {BASIS_TEXT[basis]}: найдено {len(rows)}"
             + (f", показаны {limit} последних" if len(rows) > limit else "") + ".",
             f"Сумма найденного: приход {rub(tin)}, расход {rub(tout)}" + (" (переводы в суммы не входят)" if include_transfers else "") + "."]
    for d, p, cat_name in rows[:limit]:
        bits = [f"{d:%d.%m.%Y}", ("+" if p.sign > 0 else "−") + rub(p.amount), cat_name]
        if p.project:
            bits.append(short(p.project, 50))
        if p.contractor:
            bits.append(short(p.contractor, 40))
        if p.description:
            bits.append(short(p.description, 70))
        if p.account:
            bits.append("счёт: " + short(p.account, 30))
        lines.append("- " + " · ".join(bits))
    return "\n".join(lines)


def tool_categories(adesk, args):
    include_archived = arg_bool(args, "include_archived", False)
    kinds = {1: "операционная", 2: "инвестиционная", 3: "финансовая"}
    types = {1: "доход", 2: "расход"}
    rows = sorted((c for c in adesk.categories().values() if include_archived or not c.archived), key=lambda c: (c.kind, c.type, c.name))
    lines = [f"Статей: {len(rows)}. Формат: название — тип, деятельность. В ОПиУ входит только операционная деятельность."]
    lines += [f"- {c.name} — {types.get(c.type, c.type)}, {kinds.get(c.kind, c.kind)}{' (архив)' if c.archived else ''}" for c in rows]
    return "\n".join(lines)


PERIOD_PROPS = {
    "date_from": {"type": "string", "description": "Начало периода, ГГГГ-ММ-ДД"},
    "date_to": {"type": "string", "description": "Конец периода включительно, ГГГГ-ММ-ДД (период до 366 дней)"},
    "basis": {"type": "string", "enum": ["payment", "accrual"], "default": "payment",
              "description": "payment — по дате платежа (касса), accrual — по дате начисления (как в ОПиУ)"},
}

TOOLS = [
    dict(name="adesk_accounts", title="Счета и остатки", handler=tool_accounts,
         description="Счета Adesk (банк, касса) и остаток на каждом на сейчас, итоги по юрлицам. По умолчанию только открытые счета.",
         inputSchema={"type": "object", "properties": {
             "include_closed": {"type": "boolean", "default": False, "description": "Показать и закрытые счета"}},
             "additionalProperties": False}),
    dict(name="adesk_projects", title="Проекты (объекты)", handler=tool_projects,
         description="Поиск проектов Adesk (объектов, договоров) по части названия: адрес, номер договора. Возвращает id для других инструментов.",
         inputSchema={"type": "object", "properties": {
             "query": {"type": "string", "description": "Часть названия проекта; несколько слов — должны встретиться все"},
             "category": {"type": "string", "description": "Часть названия категории проекта, например месяц «09 2026»"},
             "include_archived": {"type": "boolean", "default": False, "description": "Искать и среди архивных"},
             "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 30, "description": "Сколько строк показать"}},
             "additionalProperties": False}),
    dict(name="adesk_project_money", title="Деньги по объекту", handler=tool_project_money,
         description="Доходы и расходы по одному проекту-объекту за всё время: итоги, разница, разбивка по статьям, при желании список операций. "
                     "Укажите project_id или часть названия (query), которая находит ровно один проект.",
         inputSchema={"type": "object", "properties": {
             "project_id": {"type": "integer", "description": "id проекта из adesk_projects"},
             "query": {"type": "string", "description": "Часть названия, если id неизвестен"},
             "include_operations": {"type": "boolean", "default": False, "description": "Добавить список операций (до 50)"}},
             "additionalProperties": False}),
    dict(name="adesk_pnl_month", title="ОПиУ за месяц", handler=tool_pnl_month,
         description="Отчёт о прибылях и убытках за месяц по дате начисления: выручка, расходы по статьям, прибыль. "
                     "Только операционные статьи; переводы, кредиты, дивиденды и прочие финансовые статьи не входят. "
                     "Предупреждает, если месяц ещё не окончателен (до 11-го числа следующего месяца).",
         inputSchema={"type": "object", "properties": {
             "month": {"type": "string", "description": "Месяц ГГГГ-ММ, например 2026-08"}},
             "required": ["month"], "additionalProperties": False}),
    dict(name="adesk_totals_by_category", title="Итоги по статьям за период", handler=tool_category_totals,
         description="Расходы (или доходы) по статьям за период: сумма, доля, число операций. По умолчанию — расходы, по дате платежа, только операционные статьи.",
         inputSchema={"type": "object", "properties": {
             **PERIOD_PROPS,
             "direction": {"type": "string", "enum": ["expense", "income"], "default": "expense", "description": "expense — расходы, income — доходы"},
             "include_non_operating": {"type": "boolean", "default": False, "description": "Включить инвестиционные и финансовые статьи"}},
             "required": ["date_from", "date_to"], "additionalProperties": False}),
    dict(name="adesk_operations", title="Операции за период", handler=tool_operations,
         description="Список операций за период с фильтрами: приход/расход, статья, проект, контрагент, текст в описании, минимальная сумма. "
                     "Сначала новые; лимит строк до 200. Переводы между счетами по умолчанию скрыты.",
         inputSchema={"type": "object", "properties": {
             **PERIOD_PROPS,
             "direction": {"type": "string", "enum": ["all", "income", "expense"], "default": "all", "description": "all, income — приход, expense — расход"},
             "category": {"type": "string", "description": "Часть названия статьи"},
             "project": {"type": "string", "description": "Часть названия проекта"},
             "contractor": {"type": "string", "description": "Часть названия контрагента"},
             "text": {"type": "string", "description": "Часть описания операции"},
             "min_amount": {"type": "number", "minimum": 0, "default": 0, "description": "Минимальная сумма, ₽"},
             "include_transfers": {"type": "boolean", "default": False, "description": "Показать переводы между счетами"},
             "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50, "description": "Сколько строк показать"}},
             "required": ["date_from", "date_to"], "additionalProperties": False}),
    dict(name="adesk_categories", title="Статьи учёта", handler=tool_categories,
         description="Справочник статей Adesk: доход или расход, вид деятельности (операционная, инвестиционная, финансовая). Помогает подобрать фильтр.",
         inputSchema={"type": "object", "properties": {
             "include_archived": {"type": "boolean", "default": False, "description": "Показать и архивные статьи"}},
             "additionalProperties": False}),
]
TOOL_INDEX = {t["name"]: t for t in TOOLS}
ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

INSTRUCTIONS = ("Финансовый учёт Adesk, только чтение. Суммы в рублях. ОПиУ и прибыль — по дате начисления, "
                "только операционные статьи. Месяц окончателен с 11-го числа следующего месяца. "
                "Для объекта сначала найдите id через adesk_projects.")


# ---------------------------------------------------------------- MCP / JSON-RPC

class Server:
    def __init__(self, adesk_factory=None):
        self._adesk_factory = adesk_factory or (lambda: Adesk(AdeskClient(load_token())))
        self._adesk = None
        self.protocol = None

    def adesk(self):
        if self._adesk is None:
            self._adesk = self._adesk_factory()
        return self._adesk

    @staticmethod
    def _error(msg_id, code, message):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    def handle(self, msg):
        """Одно входящее сообщение → ответ (dict) или None для уведомлений и ответов клиента."""
        if not isinstance(msg, dict):
            return self._error(None, INVALID_REQUEST, "Ожидался JSON-объект JSON-RPC 2.0")
        msg_id = msg.get("id")
        valid_id = isinstance(msg_id, (str, int)) and not isinstance(msg_id, bool)
        safe_id = msg_id if valid_id else None
        if "method" not in msg:
            if "id" in msg and ("result" in msg or "error" in msg):
                return None  # ответ клиента на наш запрос — мы запросов не шлём
            return self._error(safe_id, INVALID_REQUEST, "Неверный запрос JSON-RPC 2.0: нет method")
        method = msg["method"]
        if msg.get("jsonrpc") != "2.0" or not isinstance(method, str) or ("id" in msg and not valid_id):
            return self._error(safe_id, INVALID_REQUEST, "Неверный запрос JSON-RPC 2.0")
        if "id" not in msg:
            return None  # notifications/initialized, notifications/cancelled и прочие — без ответа
        params = msg.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._error(msg_id, INVALID_PARAMS, "params должен быть объектом")
        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{"name": t["name"], "title": t["title"], "description": t["description"],
                                     "inputSchema": t["inputSchema"], "annotations": {"title": t["title"], **ANNOTATIONS}} for t in TOOLS]}
            elif method == "tools/call":
                result = self._call(params)
            else:
                return self._error(msg_id, METHOD_NOT_FOUND, f"Метод не поддерживается: {method}")
        except ToolInputError as e:
            return self._error(msg_id, INVALID_PARAMS, str(e))
        except Exception as e:
            print(f"{SERVER_NAME}: внутренняя ошибка в {method}: {type(e).__name__}", file=sys.stderr)
            return self._error(msg_id, INTERNAL_ERROR, "Внутренняя ошибка сервера")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _initialize(self, params):
        requested = params.get("protocolVersion")
        self.protocol = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return {"protocolVersion": self.protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "Adesk (только чтение)", "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS}

    def _call(self, params):
        name = params.get("name")
        tool = TOOL_INDEX.get(name) if isinstance(name, str) else None
        if tool is None:
            raise ToolInputError(f"Неизвестный инструмент: {name}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise ToolInputError("arguments должен быть объектом")
        unknown = set(args) - set(tool["inputSchema"]["properties"])
        try:
            if unknown:
                raise ToolInputError("Неизвестные параметры: " + ", ".join(sorted(unknown)))
            text, is_error = tool["handler"](self.adesk(), args), False
        except (ToolInputError, ConfigError, AdeskError, ReadOnlyViolation) as e:
            text, is_error = str(e), True
        except Exception as e:
            print(f"{SERVER_NAME}: сбой инструмента {name}: {type(e).__name__}", file=sys.stderr)
            text, is_error = "Внутренняя ошибка инструмента; подробности скрыты", True
        return {"content": [{"type": "text", "text": text}], "isError": is_error}


def serve(stdin=None, stdout=None, server=None):
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout
    server = server or Server()
    for raw in stdin:
        line = raw.decode("utf-8", errors="replace").strip() if isinstance(raw, bytes) else raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = Server._error(None, PARSE_ERROR, "Не удалось разобрать JSON")
        else:
            reply = server.handle(msg) if not isinstance(msg, list) else Server._error(None, INVALID_REQUEST, "Пакетные запросы не поддерживаются")
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdout.flush()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        serve()
    except KeyboardInterrupt:
        pass
    finally:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_mb = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
        print(f"{SERVER_NAME}: завершён, пик памяти {peak_mb:.1f} МБ", file=sys.stderr)


if __name__ == "__main__":
    main()
