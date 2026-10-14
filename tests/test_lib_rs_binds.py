"""db_backtest_query binds its anonymous ? parameters in SQL-text order.

No Rust toolchain runs in the unit tests, so this checks the source: the SQL is
`... WHERE <vals> ORDER BY CASE WHEN trades>=? ... LIMIT ? OFFSET ?`, so the WHERE
values must be bound before min_trades. Binding min_trades first turned
`bt.slot=?` into `bt.slot=30` and the board always came back empty.
"""
import re
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _lib_rs():
    for p in (ROOT / 'src-tauri' / 'src' / 'lib.rs', ROOT / 'lib.rs'):
        if p.exists():
            return p.read_text(encoding='utf-8')
    raise FileNotFoundError('lib.rs')


def _fn_body(src, name):
    start = src.index(f'fn {name}(')
    nxt = re.search(r'\n(?:#\[|fn |pub fn |async fn )', src[start + 1:])
    return src[start:start + 1 + nxt.start()] if nxt else src[start:]


class BacktestQueryBindOrder(unittest.TestCase):
    def test_where_values_bound_before_order_by_min_trades(self):
        body = _fn_body(_lib_rs(), 'db_backtest_query')
        sql_where = body.index('WHERE')  # sanity: the query has a WHERE built from vals
        self.assertGreater(sql_where, 0)
        bind_start = body.index('let mut bind')
        tail = body[bind_start:]
        vals_at = min(i for i in (tail.find('vals.clone()'), tail.find('extend(vals')) if i >= 0)
        min_at = tail.index('Integer(min_trades)')
        limit_at = tail.index('limit.clamp')
        self.assertLess(vals_at, min_at, 'min_trades is bound before the WHERE values')
        self.assertLess(min_at, limit_at)

    def test_sqlite_numbers_anonymous_params_in_text_order(self):
        # the property the Rust code relies on
        c = sqlite3.connect(':memory:')
        c.execute('create table bt(slot text, trades int)')
        c.executemany('insert into bt values(?,?)', [('CONFIRMATION_1', 50), ('CONFIRMATION_1', 5)])
        q = ('select trades from bt WHERE bt.slot=? '
             'ORDER BY CASE WHEN trades>=? THEN 0 ELSE 1 END LIMIT ? OFFSET ?')
        self.assertEqual(c.execute(q, (30, 'CONFIRMATION_1', 100, 0)).fetchall(), [])
        self.assertEqual(c.execute(q, ('CONFIRMATION_1', 30, 100, 0)).fetchall(), [(50,), (5,)])


if __name__ == '__main__':
    unittest.main()
