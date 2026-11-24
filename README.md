# supreme-waddle

## adesk_on_time_clients.py

Скрипт выгружает из [Adesk API v1](https://adesk.ru/api-docs/) клиентов (`/v1/contractors`), сделки (`/v1/projects`, `type = deal`) и операции поступлений (`/v1/transactions`), сравнивает плановую и фактическую даты оплат и выводит клиентов, у которых доля оплат «не позже срока» не ниже порога `ON_TIME_RATIO`.

- Срок оплаты — дата начисления операции (`relatedDate`), если её нет — дата окончания сделки (`endDate`); факт — дата операции (`date`).
- Плановые поступления (`isPlanned = true`) с прошедшей датой считаются просроченными.
- Выгрузка постраничная: параметры `start` / `length` (offset / limit в терминах Adesk API).

### Запуск

```bash
pip install requests
export ADESK_API_TOKEN="ваш_токен"   # Adesk → Настройки → Профиль → Токен API
export ON_TIME_RATIO=1.0             # 1.0 — все оплаты вовремя, 0.9 — не менее 90 %
python adesk_on_time_clients.py
```

Дополнительные параметры (`python adesk_on_time_clients.py --help`):

| Параметр | Описание |
|---|---|
| `--ratio 0.9` | порог доли оплат вовремя (перекрывает `ON_TIME_RATIO`) |
| `--grace-days 3` | допустимая задержка в днях (или env `GRACE_DAYS`) |
| `--min-payments 3` | минимальное число поступивших оплат у клиента |
| `--range-start 2025-01-01 --range-end 2025-12-31` | период операций |
| `--csv result.csv` | сохранить результат в CSV |
| `-v` | показать просроченные оплаты всех клиентов, включая не прошедших порог |
