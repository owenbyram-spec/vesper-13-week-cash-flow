"""Closed, bounded Decimal expressions; this component never runs Python code.

Numbers serialize as decimal strings. Arithmetic uses precision 38 and HALF_EVEN;
that rounding convention is a calculator contract, not an accounting policy.
The evaluation harness can call this utility; the live chat loop does not route
all arithmetic through it. Application finance services remain distinct.
"""
from __future__ import annotations

import ast
import json
import math
import re
from dataclasses import dataclass
from decimal import (Clamped, Context, Decimal, DecimalException, DivisionByZero,
                     InvalidOperation, Overflow, ROUND_HALF_EVEN, Underflow, localcontext)
from typing import Any

MAX_SOURCE_BYTES = 8192
MAX_NODES = 512
MAX_DEPTH = 32
MAX_ITEMS = 512
MAX_OPERATIONS = 2048
MAX_NAMES = 64
MAX_STRING = 512
MAX_DIGITS = 128
MAX_EXPONENT = 100
MAX_OUTPUT_BYTES = 16384
_FUNCTIONS = frozenset({"Decimal", "abs", "sum", "min", "max", "round", "mean"})
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?\Z")
_CONTEXT = Context(prec=38, rounding=ROUND_HALF_EVEN, Emin=-100, Emax=100)
for _signal in (InvalidOperation, DivisionByZero, Overflow, Underflow, Clamped):
    _CONTEXT.traps[_signal] = True


class CalculationRejected(ValueError):
    """Unsupported syntax, data or resource bounds."""


@dataclass(frozen=True)
class CalculationResult:
    success: bool
    result: Any = None
    error: str | None = None

    def to_dict(self):
        return {"success": self.success, "result": self.result, "error": self.error}


class _Evaluation:
    def __init__(self, source):
        self.source = source
        self.operations = 0
        self.items = 0
        self.names = {}

    def charge(self, count=1):
        self.operations += count
        if self.operations > MAX_OPERATIONS:
            raise CalculationRejected("Operation limit exceeded")

    def number(self, value):
        if type(value) is not Decimal or not value.is_finite():
            raise CalculationRejected("Finite numeric value required")
        if len(value.as_tuple().digits) > MAX_DIGITS or abs(value.as_tuple().exponent) > MAX_EXPONENT:
            raise CalculationRejected("Numeric precision or exponent limit exceeded")
        if value and abs(value.adjusted()) > MAX_EXPONENT:
            raise CalculationRejected("Numeric magnitude limit exceeded")
        return value

    def decimal(self, text):
        if type(text) is not str or len(text) > MAX_DIGITS or not _NUMBER.fullmatch(text):
            raise CalculationRejected("Bounded decimal literal required")
        return self.number(Decimal(text))

    def name(self, name):
        if type(name) is not str or not name.isidentifier() or name.startswith("_") or name in _FUNCTIONS:
            raise CalculationRejected("Reserved or invalid name")
        return name

    def copy_input(self, value, depth=0, seen=None):
        self.charge()
        self.items += 1
        if depth > MAX_DEPTH or self.items > MAX_ITEMS:
            raise CalculationRejected("Input depth or item limit exceeded")
        kind = type(value)
        if kind in (type(None), bool):
            return value
        if kind is str:
            if len(value) > MAX_STRING:
                raise CalculationRejected("String limit exceeded")
            return value
        if kind is int:
            if value.bit_length() > 425:
                raise CalculationRejected("Integer magnitude limit exceeded")
            return self.decimal(str(value))
        if kind is float:
            if not math.isfinite(value):
                raise CalculationRejected("Finite numeric value required")
            return self.decimal(repr(value))
        if kind not in (list, dict):
            raise CalculationRejected("Only exact JSON input types are accepted")
        if len(value) > MAX_ITEMS:
            raise CalculationRejected("Container item limit exceeded")
        seen = set() if seen is None else seen
        if id(value) in seen:
            raise CalculationRejected("Cyclic input is unsupported")
        seen.add(id(value))
        try:
            if kind is list:
                return [self.copy_input(v, depth + 1, seen) for v in value]
            if any(type(k) is not str or len(k) > MAX_STRING for k in value):
                raise CalculationRejected("Bounded string keys required")
            return {k: self.copy_input(v, depth + 1, seen) for k, v in value.items()}
        finally:
            seen.remove(id(value))

    def expression(self, node):
        self.charge()
        if type(node) is ast.Constant:
            if type(node.value) in (int, float):
                # AST float values have already lost precision: use source tokens.
                return self.decimal(ast.get_source_segment(self.source, node))
            if type(node.value) in (str, bool, type(None)):
                return self.copy_input(node.value)
        elif type(node) is ast.Name:
            if node.id not in self.names:
                raise CalculationRejected("Unknown variable")
            return self.names[node.id]
        elif type(node) in (ast.List, ast.Dict):
            self.items += len(node.elts) if type(node) is ast.List else len(node.keys)
            if self.items > MAX_ITEMS:
                raise CalculationRejected("Container item limit exceeded")
            if type(node) is ast.List:
                return [self.expression(n) for n in node.elts]
            result = {}
            for key, value in zip(node.keys, node.values):
                if key is None:
                    raise CalculationRejected("Unpacking is unsupported")
                key = self.expression(key)
                if type(key) is not str or key in result:
                    raise CalculationRejected("Unique string dictionary keys required")
                result[key] = self.expression(value)
            return result
        elif type(node) is ast.Subscript:
            value, key = self.expression(node.value), self.expression(node.slice)
            if type(value) is list and type(key) is Decimal and key == key.to_integral_value() and abs(key) <= MAX_ITEMS:
                return value[int(key)]
            if type(value) is dict and type(key) is str:
                return value[key]
            raise CalculationRejected("Exact container subscript required")
        elif type(node) is ast.UnaryOp and type(node.op) in (ast.USub, ast.UAdd):
            value = self.number(self.expression(node.operand))
            return self.number(-value if type(node.op) is ast.USub else +value)
        elif type(node) is ast.BinOp:
            left, right = self.number(self.expression(node.left)), self.number(self.expression(node.right))
            kind = type(node.op)
            if kind is ast.Add:
                value = left + right
            elif kind is ast.Sub:
                value = left - right
            elif kind is ast.Mult:
                value = left * right
            elif kind is ast.Div:
                value = left / right
            elif kind is ast.Pow:
                if right != right.to_integral_value() or abs(right) > MAX_EXPONENT:
                    raise CalculationRejected("Bounded integral power required")
                # Fixed precision and exponent bounds prevent unbounded integer work.
                value = left ** int(right)
            else:
                raise CalculationRejected("Unsupported arithmetic operation")
            return self.number(value)
        elif type(node) is ast.Call:
            if type(node.func) is not ast.Name or node.func.id not in _FUNCTIONS or node.keywords:
                raise CalculationRejected("Only fixed named calculator functions are supported")
            args = [self.expression(n) for n in node.args]
            return self.function(node.func.id, args)
        raise CalculationRejected(f"Unsupported expression: {type(node).__name__}")

    def function(self, name, args):
        if name == "Decimal" and len(args) == 1:
            return self.decimal(args[0]) if type(args[0]) is str else self.number(args[0])
        if name == "abs" and len(args) == 1:
            return self.number(abs(self.number(args[0])))
        if name == "round" and len(args) in (1, 2):
            places = self.number(args[1]) if len(args) == 2 else Decimal(0)
            if places != places.to_integral_value() or abs(places) > 38:
                raise CalculationRejected("Rounding places must be an integer within 38")
            return self.number(self.number(args[0]).quantize(Decimal(1).scaleb(-int(places))))
        if name in {"sum", "mean", "min", "max"}:
            values = args[0] if len(args) == 1 and type(args[0]) is list else args
            if not values or (name in {"sum", "mean"} and (len(args) != 1 or type(args[0]) is not list)):
                raise CalculationRejected("Nonempty numeric list required")
            self.charge(len(values))
            values = [self.number(v) for v in values]
            if name == "min":
                return min(values)
            if name == "max":
                return max(values)
            total = Decimal(0)
            for value in values:
                total = self.number(total + value)
            return self.number(total / len(values)) if name == "mean" else total
        raise CalculationRejected("Invalid calculator function arguments")

    def output(self, value, depth=0):
        self.charge()
        if depth > MAX_DEPTH:
            raise CalculationRejected("Output depth limit exceeded")
        if type(value) is Decimal:
            return str(self.number(value))
        if type(value) is list:
            return [self.output(v, depth + 1) for v in value]
        if type(value) is dict:
            return {k: self.output(v, depth + 1) for k, v in value.items()}
        return value


class FinancialCalculator:
    def evaluate(self, source: str, *, context: dict | None = None) -> CalculationResult:
        try:
            if type(source) is not str or len(source) > MAX_SOURCE_BYTES or len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
                raise CalculationRejected("Source size limit exceeded")
            tree = ast.parse(source, mode="exec")
            stack, count = [(tree, 0)], 0
            while stack:
                node, depth = stack.pop()
                count += 1
                if depth > MAX_DEPTH or count > MAX_NODES:
                    raise CalculationRejected("Syntax depth or node limit exceeded")
                stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
            evaluation = _Evaluation(source)
            with localcontext(_CONTEXT):
                if context is not None:
                    if type(context) is not dict or len(context) > MAX_NAMES:
                        raise CalculationRejected("Bounded context dictionary required")
                    evaluation.names = {evaluation.name(k): evaluation.copy_input(v) for k, v in context.items()}
                result = None
                for statement in tree.body:
                    if type(statement) is ast.Assign and len(statement.targets) == 1 and type(statement.targets[0]) is ast.Name:
                        name = evaluation.name(statement.targets[0].id)
                        if name not in evaluation.names and len(evaluation.names) >= MAX_NAMES:
                            raise CalculationRejected("Variable limit exceeded")
                        result = evaluation.expression(statement.value)
                        evaluation.names[name] = result
                    elif type(statement) is ast.Expr:
                        result = evaluation.expression(statement.value)
                    else:
                        raise CalculationRejected(f"Unsupported statement: {type(statement).__name__}")
                if not tree.body:
                    raise CalculationRejected("A calculation is required")
                output = evaluation.output(evaluation.names.get("result", result))
                if len(json.dumps(output, allow_nan=False).encode("utf-8")) > MAX_OUTPUT_BYTES:
                    raise CalculationRejected("Output size limit exceeded")
                return CalculationResult(True, output)
        except (CalculationRejected, DecimalException, SyntaxError, ValueError, TypeError,
                KeyError, IndexError, RecursionError, MemoryError) as error:
            return CalculationResult(False, error=f"Calculation rejected: {type(error).__name__}: {str(error)[:200]}")


def evaluate_financial_calculation(source, *, context=None):
    return FinancialCalculator().evaluate(source, context=context)
