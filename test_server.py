"""Тесты adesk-mcp без сети. Все данные выдуманы.

Запуск: python3 -m unittest -v test_server.py
"""
import io
import json
import os
import subprocess
import sys
import unittest
import urllib.error
import urllib.parse
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402

TOKEN = "fixture-token-0000"

CATEGORIES = [
    dict(id=1, name="Аванс", type=1, kind=1), dict(id=2, name="Финальный расчёт", type=1, kind=1),
    dict(id=3, name="Материалы", type=2, kind=1), dict(id=4, name="Зарплата", type=2, kind=1),
    dict(id=5, name="Кредит получен", type=1, kind=3), dict(id=6, name="Дивиденды", type=2, kind=3),
    dict(id=7, name="Покупка оборудования", type=2, kind=2),
]
P1 = dict(id=101, name="Объект А, договор Т-1", category=dict(id=9, name="Ремонт 08 2026"), isFinished=True, isArchived=False)
P2 = dict(id=102, name="Объект Б, договор Т-2", category=dict(id=9, name="Ремонт 09 2026"), isFinished=False, isArchived=False)
P3 = dict(id=103, name="Объект В, договор Т-3", category=dict(id=9, name="Ремонт 05 2025"), isFinished=True, isArchived=True)


def op(id, type, amount, date_, related, cat=None, project=None, parts=None, transfer=False, desc=""):
    t = dict(id=id, type=type, amount=str(amount), dateIso=date_, relatedDateIso=related, isTransfer=transfer,
             isPlanned=False, isSplitted=bool(parts), description=desc,
             category=dict(id=cat) if cat else None, project=project, contractor=dict(name="Контрагент " + str(id)),
             bankAccount=dict(name="Счёт 1"))
    if parts:
        t["parts"] = [dict(amount=str(a), category=dict(id=c), project=pr, relatedDateIso=r) for a, c, pr, r in parts]
    return t


# Август 2026 по начислению: доходы 100 000,00; расходы 30 000,50 + 20 000,00 + 5 000,00 = 55 000,50; прибыль 44 999,50.
TRANSACTIONS = [
    op(1, 1, "100000.00", "2026-07-20", "2026-08-05", cat=1, project=P1),           # оплачено в июле, начислено в августе
    op(2, 2, "30000.50", "2026-08-10", "2026-08-10", cat=3, project=P1),
    op(3, 2, "20000", "2026-09-10", "2026-08-31", cat=4),                            # зарплата задним числом
    op(4, 2, "50000", "2026-08-12", "2026-08-12", transfer=True),                    # перевод — не входит
    op(5, 1, "200000", "2026-08-15", "2026-08-15", cat=5),                           # кредит, kind 3 — не входит
    op(6, 2, "40000", "2026-08-16", "2026-08-16", cat=6),                            # дивиденды, kind 3 — не входит
    op(7, 2, "15000", "2026-08-20", "2026-08-20", parts=[("5000", 3, P1, "2026-08-20"), ("10000", 4, P2, "2026-09-01")]),
    op(8, 2, "7000", "2026-08-03", "2026-08-03"),                                    # без статьи — не входит
    op(9, 2, "3000", "2026-08-21", "2026-08-21", cat=7),                             # kind 2 — не входит
    op(10, 1, "50000", "2026-08-02", "2026-07-31", cat=2, project=P1),               # платёж в августе, начисление в июле
]
ACCOUNTS = [
    dict(name="Расчётный", bankName="Банк", legalEntity=dict(name="Юрлицо Тест"), amount="1000.10", currency="RUR", status="open"),
    dict(name="Касса", bankName="", legalEntity=dict(name="Юрлицо Тест"), amount="200", currency="RUR", status="open"),
    dict(name="Старый", bankName="Банк", legalEntity=dict(name="Юрлицо Тест"), amount="0", currency="RUR", status="closed"),
]


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeAdeskApi:
    """Имитация Adesk API. truncate=True — многодневные окна отдаются не целиком (как баг выдачи)."""

    def __init__(self, transactions=TRANSACTIONS, truncate=False, stuck=False):
        self.transactions = transactions
        self.truncate = truncate
        self.stuck = stuck
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req.get_method(), req.data, req.full_url))
        url = urllib.parse.urlsplit(req.full_url)
        path = url.path.split("/v1/", 1)[1]
        q = dict(urllib.parse.parse_qsl(url.query))
        assert q.get("api_token") == TOKEN
        if path == "transactions/categories":
            body = dict(success=True, recordsFiltered=len(CATEGORIES), categories=CATEGORIES)
        elif path == "projects":
            body = dict(success=True, recordsFiltered=3, projects=[P1, P2, P3])
        elif path == "bank-accounts":
            body = dict(success=True, recordsFiltered=len(ACCOUNTS), bankAccounts=ACCOUNTS)
        elif path == "transactions":
            body = self._transactions(q)
        else:
            raise AssertionError("неожиданный адрес " + path)
        return FakeResponse(json.dumps(body).encode())

    def _transactions(self, q):
        rows = self.transactions
        if "project" in q:
            pid = int(q["project"])
            rows = [t for t in rows if (t.get("project") or {}).get("id") == pid
                    or any((p.get("project") or {}).get("id") == pid for p in t.get("parts") or [])]
        if q.get("range") == "custom":
            s, e = q["range_start"], q["range_end"]
            if q.get("date_type") == "accrual":
                rows = [t for t in rows if any(s <= d[:10] <= e for d in
                        [t["relatedDateIso"]] + [p["relatedDateIso"] for p in t.get("parts") or []])]
            else:
                rows = [t for t in rows if s <= t["dateIso"] <= e]
        start, length = int(q.get("start", 0)), int(q.get("length", 100))
        page = rows[start:start + length]
        if self.truncate and q.get("range") == "custom" and q["range_start"] != q["range_end"] and len(page) > 0:
            page = page[:-1]
        if self.stuck and start > 0:
            page = []
        return dict(success=True, recordsFiltered=len(rows), transactions=page)


def make_server(api=None):
    api = api or FakeAdeskApi()
    srv = server.Server(adesk_factory=lambda: server.Adesk(server.AdeskClient(TOKEN, urlopen=api, sleep=lambda s: None)))
    return srv, api


def call(srv, name, **arguments):
    reply = srv.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    return reply["result"]


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.srv, self.api = make_server()

    def test_initialize_echoes_supported_version(self):
        r = self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(r["result"]["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "adesk-mcp")

    def test_initialize_unknown_version_gets_latest(self):
        r = self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(r["result"]["protocolVersion"], server.SUPPORTED_PROTOCOLS[0])

    def test_notifications_get_no_reply(self):
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}))
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "id": 5, "result": {}}))  # ответ клиента

    def test_ping(self):
        self.assertEqual(self.srv.handle({"jsonrpc": "2.0", "id": "a", "method": "ping"})["result"], {})

    def test_tools_list(self):
        tools = self.srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertTrue(5 <= len(tools) <= 7)
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(t["annotations"]["readOnlyHint"])
            self.assertFalse(t["annotations"]["destructiveHint"])
            self.assertTrue(t["description"])
        self.assertEqual(self.api.requests, [])  # список инструментов не ходит в Adesk

    def test_errors(self):
        h = self.srv.handle
        self.assertEqual(h({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}})["error"]["code"], -32602)
        self.assertEqual(h({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})["error"]["code"], -32601)
        self.assertEqual(h({"jsonrpc": "2.0", "id": 3, "method": "server/discover"})["error"]["code"], -32601)
        self.assertEqual(h({"id": 3, "method": "ping"})["error"]["code"], -32600)
        self.assertEqual(h({"jsonrpc": "2.0", "id": None, "method": "ping"})["error"]["code"], -32600)
        self.assertEqual(h({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": []})["error"]["code"], -32602)
        self.assertEqual(h([1])["error"]["code"], -32600)

    def test_tool_input_errors_are_tool_results(self):
        for name, args in (("adesk_pnl_month", {"month": "август"}), ("adesk_pnl_month", {}),
                           ("adesk_operations", {"date_from": "2026-08-01", "date_to": "2027-12-31"}),
                           ("adesk_operations", {"date_from": "2026-08-01", "date_to": "2026-08-31", "limit": 5000}),
                           ("adesk_accounts", {"unknown": 1})):
            with self.subTest(name=name, args=args):
                r = call(self.srv, name, **args)
                self.assertTrue(r["isError"])

    def test_stdio_framing(self):
        out = io.StringIO()
        lines = [b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}\n',
                 b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n', b"\n", b"{not json\n",
                 b'[{"jsonrpc":"2.0","id":9,"method":"ping"}]\n',
                 b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"adesk_pnl_month","arguments":{"month":"2026-08"}}}\n']
        server.serve(stdin=iter(lines), stdout=out, server=self.srv)
        replies = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([r.get("id") for r in replies], [1, None, None, 2])
        self.assertEqual(replies[1]["error"]["code"], -32700)
        self.assertEqual(replies[2]["error"]["code"], -32600)
        self.assertIn("Прибыль: 44 999,50 ₽", replies[3]["result"]["content"][0]["text"])
        self.assertEqual(len(out.getvalue().splitlines()), 4)  # многострочный текст не рвёт кадр

    def test_subprocess_without_token(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("ADESK_")}
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "adesk_accounts", "arguments": {}}}]
        proc = subprocess.run([sys.executable, str(Path(server.__file__))], input="".join(json.dumps(m) + "\n" for m in msgs),
                              capture_output=True, text=True, env=env, timeout=30)
        replies = [json.loads(x) for x in proc.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertTrue(replies[2]["result"]["isError"])
        self.assertIn("ADESK_API_TOKEN", replies[2]["result"]["content"][0]["text"])
        self.assertIn("пик памяти", proc.stderr)


class ReadOnlyTests(unittest.TestCase):
    def test_non_get_rejected_before_network(self):
        api = FakeAdeskApi()
        client = server.AdeskClient(TOKEN, urlopen=api)
        for method in ("POST", "PUT", "PATCH", "DELETE", "get", ""):
            with self.subTest(method=method), self.assertRaises(server.ReadOnlyViolation):
                client._request(method, "transactions", {})
        for path in ("transactions/create", "contractors", "transaction/1/delete", "../projects"):
            with self.subTest(path=path), self.assertRaises(server.ReadOnlyViolation):
                client.get(path)
        self.assertEqual(api.requests, [])

    def test_client_has_only_get(self):
        public = [m for m in dir(server.AdeskClient) if not m.startswith("_")]
        self.assertEqual(public, ["get"])

    def test_every_tool_sends_only_get(self):
        srv, api = make_server()
        for name, args in (("adesk_accounts", {}), ("adesk_projects", {"query": "договор"}),
                           ("adesk_project_money", {"project_id": 101, "include_operations": True}),
                           ("adesk_pnl_month", {"month": "2026-08"}), ("adesk_categories", {}),
                           ("adesk_totals_by_category", {"date_from": "2026-08-01", "date_to": "2026-08-31"}),
                           ("adesk_operations", {"date_from": "2026-08-01", "date_to": "2026-08-31"})):
            with self.subTest(tool=name):
                self.assertFalse(call(srv, name, **args)["isError"])
        self.assertTrue(api.requests)
        self.assertTrue(all(m == "GET" and body is None for m, body, _ in api.requests))

    def test_token_never_leaks_in_errors(self):
        def failing(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden " + TOKEN, {}, None)

        def echoing(req, timeout=None):
            return FakeResponse(json.dumps({"success": False, "errors": {"api_token": [TOKEN + " неверный"]}}).encode())

        for opener in (failing, echoing):
            srv = server.Server(adesk_factory=lambda o=opener: server.Adesk(server.AdeskClient(TOKEN, urlopen=o, sleep=lambda s: None)))
            r = call(srv, "adesk_accounts")
            self.assertTrue(r["isError"])
            self.assertNotIn(TOKEN, json.dumps(r, ensure_ascii=False))


class PnlTests(unittest.TestCase):
    def setUp(self):
        self.srv, self.api = make_server()

    def test_pnl_pure(self):
        cats = {c["id"]: server.Category(c["id"], c["name"], c["type"], c["kind"], False) for c in CATEGORIES}
        parts = [p for t in TRANSACTIONS for p in server.parse_operation(t)]
        r = server.pnl(parts, cats, "2026-08")
        self.assertEqual(r["income_total"], Decimal("100000.00"))
        self.assertEqual(r["expense_total"], Decimal("55000.50"))
        self.assertEqual(r["profit"], Decimal("44999.50"))
        self.assertEqual(r["expense"], {"Материалы": Decimal("35000.50"), "Зарплата": Decimal("20000")})
        self.assertEqual(r["other"], {"Кредит получен": Decimal("200000"), "Дивиденды": Decimal("-40000"),
                                      "Покупка оборудования": Decimal("-3000")})
        self.assertEqual((r["transfers"], r["uncategorized"], r["uncategorized_sum"]), (1, 1, Decimal("-7000")))
        july = server.pnl(parts, cats, "2026-07")
        self.assertEqual(july["profit"], Decimal("50000"))
        sept = server.pnl(parts, cats, "2026-09")
        self.assertEqual(sept["expense_total"], Decimal("10000"))  # вторая часть разбитой операции

    def test_split_sum_mismatch_rejected(self):
        bad = op(99, 2, "100", "2026-08-01", "2026-08-01", parts=[("60", 3, None, "2026-08-01"), ("30", 4, None, "2026-08-01")])
        with self.assertRaises(server.AdeskError):
            server.parse_operation(bad)

    def test_pnl_tool_and_finality(self):
        text = call(self.srv, "adesk_pnl_month", month="2026-8")["content"][0]["text"]
        self.assertIn("Выручка (операционные доходы): 100 000,00 ₽", text)
        self.assertIn("Расходы: 55 000,50 ₽", text)
        self.assertIn("Прибыль: 44 999,50 ₽", text)
        self.assertIn("Месяц окончателен.", text)
        self.assertIn("без статьи — 1 шт.", text)
        self.assertTrue(any("date_type=accrual" in url for _, _, url in self.api.requests))

    def test_month_finality_rule(self):
        self.assertEqual(server.month_is_final("2026-09", date(2026, 10, 10)), (False, date(2026, 10, 11)))
        self.assertEqual(server.month_is_final("2026-09", date(2026, 10, 11)), (True, date(2026, 10, 11)))
        self.assertEqual(server.month_is_final("2026-12", date(2027, 1, 5)), (False, date(2027, 1, 11)))
        text = server.tool_pnl_month(server.Adesk(server.AdeskClient(TOKEN, urlopen=FakeAdeskApi())), {"month": "2026-09"}, today=date(2026, 10, 10))
        self.assertIn("ещё не окончателен", text)
        self.assertIn("11.10.2026", text)


class PaginationTests(unittest.TestCase):
    def test_truncated_pages_are_completed(self):
        many = [op(1000 + i, 2, "1", "2026-08-%02d" % (1 + i % 2), "2026-08-01", cat=3) for i in range(250)]
        api = FakeAdeskApi(transactions=many, truncate=True)
        adesk = server.Adesk(server.AdeskClient(TOKEN, urlopen=api, sleep=lambda s: None))
        parts = adesk.operations(date(2026, 8, 1), date(2026, 8, 4))
        self.assertEqual(sorted(p.op_id for p in parts), sorted(t["id"] for t in many))
        self.assertTrue(any("start=100" in url for _, _, url in api.requests))  # дочитано страницами внутри дня

    def test_stuck_day_raises(self):
        many = [op(2000 + i, 2, "1", "2026-08-01", "2026-08-01", cat=3) for i in range(150)]
        adesk = server.Adesk(server.AdeskClient(TOKEN, urlopen=FakeAdeskApi(transactions=many, stuck=True), sleep=lambda s: None))
        with self.assertRaises(server.AdeskError):
            adesk.operations(date(2026, 8, 1), date(2026, 8, 1))

    def test_accrual_windows_do_not_double_count(self):
        adesk = server.Adesk(server.AdeskClient(TOKEN, urlopen=FakeAdeskApi(), sleep=lambda s: None))
        parts = adesk.operations(date(2026, 8, 1), date(2026, 9, 30), basis="accrual")
        ids = [p.op_id for p in parts]
        self.assertEqual(ids.count(7), 2)  # две части разбитой операции, сама операция один раз
        self.assertEqual(len(set(ids)), len(TRANSACTIONS) - 1)  # операция 10 начислена в июле


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.srv, _ = make_server()

    def text(self, name, **args):
        r = call(self.srv, name, **args)
        self.assertFalse(r["isError"], r)
        return r["content"][0]["text"]

    def test_accounts(self):
        t = self.text("adesk_accounts")
        self.assertIn("Итого: 1 200,10 ₽", t)
        self.assertNotIn("Старый", t)
        self.assertIn("Старый", self.text("adesk_accounts", include_closed=True))

    def test_projects_search(self):
        t = self.text("adesk_projects", query="ДОГОВОР т-2")
        self.assertIn("Проектов найдено: 1", t)
        self.assertIn("id 102", t)
        self.assertIn("Проектов найдено: 1", self.text("adesk_projects", category="08 2026"))
        self.assertIn("id 103", self.text("adesk_projects", query="Т-3", include_archived=True))

    def test_project_money(self):
        t = self.text("adesk_project_money", project_id=101)
        self.assertIn("Доходы: 150 000,00 ₽", t)
        self.assertIn("Расходы: 35 000,50 ₽", t)  # 30 000,50 + своя часть разбитой операции 5 000
        self.assertIn("Разница (доходы − расходы): 114 999,50 ₽", t)
        self.assertIn("найдено проектов: 3", self.text("adesk_project_money", query="Объект"))  # архивные тоже

    def test_totals_and_operations(self):
        t = self.text("adesk_totals_by_category", date_from="2026-08-01", date_to="2026-08-31")
        self.assertIn("Итого: 45 000,50 ₽", t)  # по дате платежа: 30 000,50 + обе части разбитой 5 000 + 10 000
        self.assertIn("- Материалы: 35 000,50 ₽", t)
        self.assertIn("(77,8 %, операций: 2)", t)  # 35 000,50 / 45 000,50
        t = self.text("adesk_operations", date_from="2026-08-01", date_to="2026-08-31", direction="expense", category="материал")
        self.assertIn("найдено 2", t)
        t = self.text("adesk_operations", date_from="2026-08-01", date_to="2026-08-31", include_transfers=True, min_amount=40000, limit=1)
        self.assertIn("показаны 1 последних", t)

    def test_categories(self):
        t = self.text("adesk_categories")
        self.assertIn("Дивиденды — расход, финансовая", t)


class ConfigTests(unittest.TestCase):
    def test_env_token(self):
        self.assertEqual(server.load_token({"ADESK_API_TOKEN": " abc "}), "abc")

    def test_env_file(self):
        with TemporaryDirectory() as d:
            p = Path(d, ".env")
            p.write_text('# comment\nOTHER=1\nexport ADESK_API_TOKEN="abc def"  # note\n')
            self.assertEqual(server.load_token({"ADESK_ENV_FILE": str(p)}), "abc def")
            p.write_text("OTHER=1\n")
            with self.assertRaises(server.ConfigError):
                server.load_token({"ADESK_ENV_FILE": str(p)})
        with self.assertRaises(server.ConfigError):
            server.load_token({"ADESK_ENV_FILE": "/nonexistent/.env"})
        with self.assertRaises(server.ConfigError):
            server.load_token({})


if __name__ == "__main__":
    unittest.main()
