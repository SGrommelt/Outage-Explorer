"""A deliberately small, structurally validated read-only SQL API."""

from datetime import date
from decimal import Decimal, DecimalException

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

from outage_explorer.api.data_source import open_data
from outage_explorer.analytics import AnalyticsError, open_analytics
from outage_explorer.api.datasets import ColumnInfo, DATASETS, _response, _unavailable
from outage_explorer.auth import User, authorize_datasets, require_sql

router = APIRouter(tags=['query'])
MAX_QUERY_ROWS = 200
CORE_COLUMNS = frozenset(('outage_date', 'capacity_mw', 'outage_mw', 'percent_outage'))
COLUMNS = {
    'national_outages': CORE_COLUMNS,
    'facility_outages': CORE_COLUMNS | {'facility_id', 'facility_name'},
    'generator_outages': CORE_COLUMNS | {'facility_id', 'facility_name', 'generator_id'},
}


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sql: str = Field(min_length=1, max_length=10_000)


class QueryResponse(BaseModel):
    columns: list[ColumnInfo]
    rows: list[list[date | Decimal | str | None]]
    row_count: int
    row_limit: int
    truncated: bool


def _invalid() -> HTTPException:
    return HTTPException(400, 'Invalid or unsupported query.')


def _only(node: exp.Expression, keys: set[str]) -> None:
    if any(value is not None and value != [] and key not in keys for key, value in node.args.items()):
        raise _invalid()


def prepare_query(sql: str, user: User) -> tuple[str, list]:
    """Compile a safe AST subset; never execute/render the original user SQL."""
    try:
        statements = sqlglot.parse(sql, read='duckdb', error_level=ErrorLevel.RAISE,
                                   error_message_context=0)  # No raw query in fallback warnings.
        # SQLGlot represents a trailing comment after ';' as an empty Semicolon.
        statements = [s for s in statements if not (type(s) is exp.Semicolon and not s.args)]
        if len(statements) != 1 or type(statements[0]) is not exp.Select:
            raise _invalid()
        tree = statements[0]
        if sum(1 for _ in tree.walk()) > 512:
            raise _invalid()
        _only(tree, {'expressions', 'from_', 'where', 'order', 'limit'})
        source = tree.args.get('from_')
        if type(source) is not exp.From:
            raise _invalid()
        _only(source, {'this'})
        table = source.this
        if type(table) is not exp.Table or type(table.this) is not exp.Identifier:
            raise _invalid()
        _only(table, {'this'})
        if len(list(tree.find_all(exp.Table))) != 1 or len(list(tree.find_all(exp.Select))) != 1:
            raise _invalid()
        name = table.name
        if name not in DATASETS:
            raise HTTPException(403, 'Forbidden.')
        authorize_datasets(user, (name,))
        columns = COLUMNS[name]
        parameters = []

        def column(node):
            if type(node) is not exp.Column or type(node.this) is not exp.Identifier:
                raise _invalid()
            _only(node, {'this'})  # No qualified names, dots, stars, or implicit struct access.
            canonical = node.name.lower()
            if canonical not in columns:
                raise _invalid()
            return '"' + canonical + '"'  # Fixed canonical column, never raw input.

        def bind(value):
            parameters.append(value)
            return '?'

        operators = {exp.EQ: '=', exp.NEQ: '<>', exp.GT: '>', exp.GTE: '>=',
                     exp.LT: '<', exp.LTE: '<=', exp.And: 'AND', exp.Or: 'OR',
                     exp.Like: 'LIKE', exp.Is: 'IS'}

        def expression(node):
            kind = type(node)
            if kind is exp.Column:
                return column(node)
            if kind is exp.Literal:
                _only(node, {'this', 'is_string'})
                if node.is_string:
                    return bind(node.this)
                value = Decimal(node.this)
                if not value.is_finite() or value.adjusted() >= 18 or value.as_tuple().exponent < -6:
                    raise _invalid()
                return bind(value)
            if kind is exp.Null:
                _only(node, set())
                return 'NULL'
            if kind is exp.Boolean:
                _only(node, {'this'})
                return 'TRUE' if node.this else 'FALSE'
            if kind is exp.Cast:
                _only(node, {'this', 'to'})
                if (type(node.this) is not exp.Literal or not node.this.is_string
                        or type(node.args.get('to')) is not exp.DataType
                        or node.args['to'].this != exp.DataType.Type.DATE):
                    raise _invalid()
                _only(node.args['to'], {'this', 'nested'})
                if node.args['to'].args.get('nested'):
                    raise _invalid()
                return bind(date.fromisoformat(node.this.this))
            if kind in (exp.Paren, exp.Not, exp.Neg):
                _only(node, {'this'})
                if kind is exp.Neg and (type(node.this) is not exp.Literal or node.this.is_string):
                    raise _invalid()
                prefix = {exp.Paren: '', exp.Not: 'NOT ', exp.Neg: '-'}[kind]
                return '(' + prefix + expression(node.this) + ')'
            if kind in operators:
                _only(node, {'this', 'expression'})
                return '(' + expression(node.this) + ' ' + operators[kind] + ' ' + expression(node.expression) + ')'
            if kind is exp.Between:
                _only(node, {'this', 'low', 'high'})
                return '(' + expression(node.this) + ' BETWEEN ' + expression(node.args['low']) + ' AND ' + expression(node.args['high']) + ')'
            if kind is exp.In:
                _only(node, {'this', 'expressions'})
                values = node.expressions
                if not values or len(values) > 100 or any(type(v) not in (exp.Literal, exp.Null) for v in values):
                    raise _invalid()
                return '(' + expression(node.this) + ' IN (' + ', '.join(expression(v) for v in values) + '))'
            # Every function, subquery, arithmetic operator, and unrecognized node fails closed.
            raise _invalid()

        projections = tree.expressions
        if len(projections) == 1 and type(projections[0]) is exp.Star:
            _only(projections[0], set())
            projection = '*'
        elif 1 <= len(projections) <= 32:
            projection = ', '.join(column(item) for item in projections)
        else:
            raise _invalid()
        query = 'SELECT ' + projection + ' FROM (' + DATASETS[name].query + ') AS approved'
        where = tree.args.get('where')
        if where is not None:
            _only(where, {'this'})
            query += ' WHERE ' + expression(where.this)
        order = tree.args.get('order')
        if order is None:
            query += ' ORDER BY ' + DATASETS[name].order
        else:
            _only(order, {'expressions'})
            if not order.expressions:
                raise _invalid()
            items = []
            for item in order.expressions:
                if type(item) is not exp.Ordered:
                    raise _invalid()
                _only(item, {'this', 'desc', 'nulls_first'})
                items.append(column(item.this) + (' DESC' if item.args.get('desc') else ' ASC')
                             + (' NULLS FIRST' if item.args.get('nulls_first') else ' NULLS LAST'))
            query += ' ORDER BY ' + ', '.join(items)
        fetch_limit = MAX_QUERY_ROWS + 1
        limit = tree.args.get('limit')
        if limit is not None:
            _only(limit, {'expression'})
            value = limit.expression
            if type(value) is not exp.Literal or not value.is_int or not 0 <= int(value.this) <= 2**63 - 1:
                raise _invalid()
            fetch_limit = min(fetch_limit, int(value.this))
        query += ' LIMIT ?'
        parameters.append(fetch_limit)
        return query, parameters
    except (SqlglotError, DecimalException, ValueError, TypeError, KeyError, RecursionError):
        raise _invalid() from None


@router.post('/query', response_model=QueryResponse)
def query(request: Request, body: QueryRequest, user: User = Depends(require_sql)) -> JSONResponse:
    sql, parameters = prepare_query(body.sql, user)  # RBAC + AST checks precede snapshot/connection access.
    try:
        with open_data(request, open_analytics) as db:
            cursor = db.execute(sql, parameters)
            columns = [ColumnInfo(name=c[0], type=str(c[1])) for c in cursor.description]
            rows = cursor.fetchmany(MAX_QUERY_ROWS + 1)
    except (AnalyticsError, duckdb.IOException, OSError):
        raise _unavailable() from None
    except duckdb.Error:
        raise _invalid() from None
    return _response(QueryResponse(columns=columns, rows=rows[:MAX_QUERY_ROWS],
                                   row_count=min(len(rows), MAX_QUERY_ROWS), row_limit=MAX_QUERY_ROWS,
                                   truncated=len(rows) > MAX_QUERY_ROWS))
