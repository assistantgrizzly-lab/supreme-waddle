#!/usr/bin/env python3
"""Клиенты Adesk, которые платят вовремя.

Скрипт выгружает из Adesk API v1 контрагентов (клиентов), сделки и операции
поступлений, сравнивает плановую и фактическую даты оплат и выводит клиентов,
у которых доля оплат «не позже срока» >= ON_TIME_RATIO.

Соответствие терминов документации https://adesk.ru/api-docs/:
  * клиенты  -> GET /v1/contractors   (поле ответа "contractors")
  * сделки   -> GET /v1/projects      (поле "projects", type = "deal" / "product_deal")
  * операции -> GET /v1/transactions  (поле "transactions")
  * постраничность: параметры start (offset) и length (limit),
    общее число записей — recordsFiltered.

Логика сравнения дат по каждой операции поступления клиента:
  * фактическая операция (isPlanned = false): факт = date, срок = relatedDate
    (дата начисления), если её нет — endDate сделки, к которой привязана
    операция. Оплата вовремя, если date <= срок + GRACE_DAYS.
    Операции без срока в расчёт не попадают.
  * плановая операция (isPlanned = true) с date + GRACE_DAYS < сегодня —
    оплата не поступила в срок (просрочка).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterator

import requests

BASE_URL = "https://api.adesk.ru/v1"
DEAL_TYPES = {"deal", "product_deal"}
DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%Y.%m.%d", "%d.%m.%Y %H:%M")


class AdeskError(RuntimeError):
    pass


def parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


class AdeskClient:
    def __init__(self, token: str, base_url: str = BASE_URL, page_size: int = 100,
                 timeout: float = 30, max_retries: int = 5,
                 session: requests.Session | None = None):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = {**(params or {}), "api_token": self.token}
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(self.max_retries + 1):
            resp = self.session.get(url, params=query, timeout=self.timeout)
            if resp.status_code == 429 and attempt < self.max_retries:
                time.sleep(2 ** attempt)
                continue
            try:
                data = resp.json()
            except ValueError:
                raise AdeskError(f"{path}: HTTP {resp.status_code}, ответ не JSON") from None
            if resp.status_code >= 400 or not data.get("success", False):
                if data.get("code") == 429 and attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise AdeskError(f"{path}: HTTP {resp.status_code}, "
                                 f"{data.get('message') or data.get('errors') or data}")
            return data
        raise AdeskError(f"{path}: превышено число повторов")

    def paginate(self, path: str, key: str,
                 params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """Постраничная выгрузка через start (offset) / length (limit)."""
        start = 0
        seen: set[Any] = set()
        while True:
            data = self.get(path, {**(params or {}), "start": start, "length": self.page_size})
            items = data.get(key) or []
            new_items = [i for i in items if i.get("id") not in seen]
            if not new_items:
                return
            for item in new_items:
                seen.add(item.get("id"))
                yield item
            start += len(items)
            total = data.get("recordsFiltered")
            if len(items) < self.page_size or (isinstance(total, int) and start >= total):
                return

    def contractors(self) -> list[dict[str, Any]]:
        return list(self.paginate("contractors", "contractors"))

    def deals(self) -> list[dict[str, Any]]:
        projects = self.paginate("projects", "projects", {"status": "all"})
        return [p for p in projects if p.get("type") in DEAL_TYPES]

    def income_transactions(self, range_start: str | None = None,
                            range_end: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"type": "income", "status": "all"}
        if range_start or range_end:
            params.update(range="custom",
                          range_start=range_start or "2000-01-01",
                          range_end=range_end or date.today().isoformat())
        return list(self.paginate("transactions", "transactions", params))


@dataclass
class ClientStats:
    client_id: Any
    name: str
    on_time: int = 0
    late: int = 0
    paid: int = 0
    late_details: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.on_time + self.late

    @property
    def ratio(self) -> float:
        return self.on_time / self.total if self.total else 0.0


def _id(obj: Any) -> Any:
    return obj.get("id") if isinstance(obj, dict) else obj


def evaluate(contractors: list[dict[str, Any]], deals: list[dict[str, Any]],
             transactions: list[dict[str, Any]], today: date | None = None,
             grace_days: int = 0) -> dict[Any, ClientStats]:
    today = today or date.today()
    grace = timedelta(days=grace_days)
    names = {c["id"]: c.get("name") or str(c["id"]) for c in contractors if "id" in c}
    deals_by_id = {d["id"]: d for d in deals if "id" in d}
    stats: dict[Any, ClientStats] = {}

    def client_stats(cid: Any, fallback_name: str | None) -> ClientStats:
        if cid not in stats:
            stats[cid] = ClientStats(cid, names.get(cid) or fallback_name or str(cid))
        return stats[cid]

    for tx in transactions:
        if tx.get("isTransfer"):
            continue
        deal = deals_by_id.get(_id(tx.get("project")))
        contractor = tx.get("contractor") or (deal or {}).get("dealContractor")
        cid = _id(contractor)
        if cid is None:
            continue
        fallback = contractor.get("name") if isinstance(contractor, dict) else None
        tx_date = parse_date(tx.get("date")) or parse_date(tx.get("dateIso"))
        if tx_date is None:
            continue

        if tx.get("isPlanned"):
            if tx_date + grace < today:
                s = client_stats(cid, fallback)
                s.late += 1
                s.late_details.append(f"операция {tx.get('id')}: план {tx_date}, оплаты нет")
            continue

        due = parse_date(tx.get("relatedDate")) or parse_date((deal or {}).get("endDate"))
        if due is None:
            continue
        s = client_stats(cid, fallback)
        s.paid += 1
        if tx_date <= due + grace:
            s.on_time += 1
        else:
            s.late += 1
            s.late_details.append(f"операция {tx.get('id')}: срок {due}, оплата {tx_date}")
    return stats


def whole_days(raw: str) -> int:
    value = float(raw)
    if not value.is_integer() or not 0 <= value <= 36500:
        raise ValueError(f"ожидается целое число дней от 0 до 36500: {raw!r}")
    return int(value)


def resolve_option(parser: argparse.ArgumentParser, value: Any, env_name: str,
                   default: Any, cast: Any) -> Any:
    if value is not None:
        return value
    raw = os.getenv(env_name)
    if raw in (None, ""):
        return default
    try:
        return cast(raw)
    except ValueError:
        parser.error(f"некорректное значение {env_name}={raw!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Клиенты Adesk, оплачивающие вовремя.")
    parser.add_argument("--ratio", type=float,
                        help="минимальная доля оплат вовремя, 0..1 (env ON_TIME_RATIO, по умолчанию 1.0)")
    parser.add_argument("--min-payments", type=int, default=1,
                        help="минимальное число поступивших оплат клиента (по умолчанию 1)")
    parser.add_argument("--grace-days", type=whole_days,
                        help="допустимая задержка в днях (env GRACE_DAYS, по умолчанию 0)")
    parser.add_argument("--range-start", help="начало периода операций, YYYY-MM-DD")
    parser.add_argument("--range-end", help="конец периода операций, YYYY-MM-DD")
    parser.add_argument("--page-size", type=int, default=100, help="размер страницы (length)")
    parser.add_argument("--csv", help="сохранить результат в CSV-файл")
    parser.add_argument("-v", "--verbose", action="store_true", help="показать просрочки по клиентам")
    args = parser.parse_args(argv)
    args.ratio = resolve_option(parser, args.ratio, "ON_TIME_RATIO", 1.0, float)
    args.grace_days = resolve_option(parser, args.grace_days, "GRACE_DAYS", 0, whole_days)

    token = os.getenv("ADESK_API_TOKEN")
    if not token:
        print("Ошибка: задайте переменную окружения ADESK_API_TOKEN", file=sys.stderr)
        return 2
    if not 0 <= args.ratio <= 1:
        print("Ошибка: ON_TIME_RATIO/--ratio должен быть в диапазоне 0..1", file=sys.stderr)
        return 2

    api = AdeskClient(token, page_size=args.page_size)
    try:
        contractors = api.contractors()
        deals = api.deals()
        transactions = api.income_transactions(args.range_start, args.range_end)
    except (AdeskError, requests.RequestException) as exc:
        print(f"Ошибка Adesk API: {exc}", file=sys.stderr)
        return 1

    stats = evaluate(contractors, deals, transactions, grace_days=args.grace_days)
    good = sorted(
        (s for s in stats.values() if s.paid >= args.min_payments and s.ratio >= args.ratio),
        key=lambda s: (-s.ratio, -s.total, s.name.lower()),
    )

    print(f"Клиентов: {len(contractors)}, сделок: {len(deals)}, операций поступлений: {len(transactions)}")
    print(f"Порог ON_TIME_RATIO = {args.ratio:.0%}; подходят {len(good)} из {len(stats)} клиентов с оплатами\n")
    print(f"{'ID':>8}  {'Клиент':<40} {'Вовремя':>8} {'Всего':>6} {'Доля':>6}")
    for s in good:
        print(f"{s.client_id!s:>8}  {s.name[:40]:<40} {s.on_time:>8} {s.total:>6} {s.ratio:>6.0%}")

    if args.verbose:
        late_clients = sorted((s for s in stats.values() if s.late_details),
                              key=lambda s: s.name.lower())
        print(f"\nПросроченные оплаты ({len(late_clients)} клиентов):")
        for s in late_clients:
            print(f"{s.client_id!s:>8}  {s.name[:40]:<40} вовремя {s.on_time} из {s.total} ({s.ratio:.0%})")
            for line in s.late_details:
                print(f"{'':>10}- {line}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["client_id", "name", "on_time", "total", "ratio"])
            for s in good:
                writer.writerow([s.client_id, s.name, s.on_time, s.total, f"{s.ratio:.4f}"])
        print(f"\nCSV сохранён: {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
