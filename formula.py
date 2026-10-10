# hexcel formula language.
#
# grammar:
#   expr    = term (('+' | '-') term)*
#   term    = pow  (('*' | '/') pow )*
#   pow     = unary ('^' pow)?             # right-associative
#   unary   = ('+' | '-') unary | primary
#   primary = NUMBER | '(' expr ')' | ref
#   ref     = rel | abs
#   rel     = '{' SIGNED_INT ',' SIGNED_INT ',' SIGNED_INT '}'
#   abs     = '$' rel

import re
from dataclasses import dataclass, field
from typing import Union

######################################################################################### errors

class ParseError(Exception): pass
class EvalError(Exception):  pass

######################################################################################### tokens

@dataclass
class Token:
	kind: str
	text: str
	pos:  int

_TOKEN_RE = re.compile(r"""
	(?P<WS>\s+)                           |
	(?P<NUMBER>\d+\.\d+|\.\d+|\d+\.?)     |
	(?P<PLUS>\+)                          |
	(?P<MINUS>-)                          |
	(?P<STAR>\*)                          |
	(?P<SLASH>/)                          |
	(?P<CARET>\^)                         |
	(?P<LPAREN>\()                        |
	(?P<RPAREN>\))                        |
	(?P<LBRACE>\{)                        |
	(?P<RBRACE>\})                        |
	(?P<COMMA>,)                          |
	(?P<DOLLAR>\$)
""", re.VERBOSE)

def tokenize(text):
	toks = []
	i = 0
	while i < len(text):
		m = _TOKEN_RE.match(text, i)
		if not m:
			raise ParseError(f"unexpected character {text[i]!r} at {i}")
		if m.lastgroup != "WS":
			toks.append(Token(m.lastgroup, m.group(), i))
		i = m.end()
	toks.append(Token("EOF", "", len(text)))
	return toks

#################################################################################### ast

@dataclass
class Num:
	value: float

@dataclass
class BinOp:
	op:    str
	left:  "Expr"
	right: "Expr"

@dataclass
class UnOp:
	op:      str
	operand: "Expr"

@dataclass
class Rel:
	dx: int
	dy: int
	dz: int

@dataclass
class Abs:
	x: int
	y: int
	z: int

Expr = Union[Num, BinOp, UnOp, Rel, Abs]

@dataclass
class EvalCtx:
	# cube cell owning the formula being evaluated.
	focus: tuple
	# raw cells dict, keyed by (x, y, z).
	cells: dict
	# set of cells currently on the recursion stack; cycle detection.
	visiting: set = field(default_factory=set)
	# memoized cell values; caller invalidates on cell content change.
	cache: dict = field(default_factory=dict)

######################################################################################### parser

class _Parser:
	def __init__(self, toks):
		self.toks = toks
		self.pos = 0

	def peek(self):
		return self.toks[self.pos]

	def eat(self, kind):
		t = self.toks[self.pos]
		if t.kind != kind:
			raise ParseError(f"expected {kind}, got {t.kind} ({t.text!r}) at {t.pos}")
		self.pos += 1
		return t

	def parse_expr(self):
		node = self.parse_term()
		while self.peek().kind in ("PLUS", "MINUS"):
			op = self.eat(self.peek().kind).text
			node = BinOp(op, node, self.parse_term())
		return node

	def parse_term(self):
		node = self.parse_pow()
		while self.peek().kind in ("STAR", "SLASH"):
			op = self.eat(self.peek().kind).text
			node = BinOp(op, node, self.parse_pow())
		return node

	def parse_pow(self):
		node = self.parse_unary()
		if self.peek().kind == "CARET":
			self.eat("CARET")
			node = BinOp("^", node, self.parse_pow())
		return node

	def parse_unary(self):
		if self.peek().kind in ("PLUS", "MINUS"):
			op = self.eat(self.peek().kind).text
			return UnOp(op, self.parse_unary())
		return self.parse_primary()

	def parse_primary(self):
		t = self.peek()
		if t.kind == "NUMBER":
			self.eat("NUMBER")
			return Num(float(t.text))
		if t.kind == "LPAREN":
			self.eat("LPAREN")
			node = self.parse_expr()
			self.eat("RPAREN")
			return node
		if t.kind == "DOLLAR":
			self.eat("DOLLAR")
			x, y, z = self.parse_coord_triple()
			if x + y + z != 0:
				raise ParseError(f"absolute coord must sum to 0: {x}+{y}+{z}={x+y+z}")
			return Abs(x, y, z)
		if t.kind == "LBRACE":
			dx, dy, dz = self.parse_coord_triple()
			if dx + dy + dz != 0:
				raise ParseError(f"relative delta must sum to 0: {dx}+{dy}+{dz}={dx+dy+dz}")
			return Rel(dx, dy, dz)
		raise ParseError(f"unexpected token {t.kind} ({t.text!r}) at {t.pos}")

	def parse_coord_triple(self):
		self.eat("LBRACE")
		a = self.parse_int()
		self.eat("COMMA")
		b = self.parse_int()
		self.eat("COMMA")
		c = self.parse_int()
		self.eat("RBRACE")
		return a, b, c

	def parse_int(self):
		sign = 1
		if self.peek().kind == "PLUS":
			self.eat("PLUS")
		elif self.peek().kind == "MINUS":
			self.eat("MINUS")
			sign = -1
		t = self.eat("NUMBER")
		try:
			f = float(t.text)
			if f != int(f):
				raise ValueError
			return sign * int(f)
		except ValueError:
			raise ParseError(f"expected integer coord at {t.pos}, got {t.text!r}")

def parse(text):
	p = _Parser(tokenize(text))
	node = p.parse_expr()
	if p.peek().kind != "EOF":
		raise ParseError(f"trailing tokens starting at {p.peek().pos}")
	return node

############################################################################## evaluator

def evaluate(node, ctx):
	if isinstance(node, Num):
		return node.value
	if isinstance(node, UnOp):
		v = evaluate(node.operand, ctx)
		return v if node.op == "+" else -v
	if isinstance(node, BinOp):
		a = evaluate(node.left, ctx)
		b = evaluate(node.right, ctx)
		if node.op == "+": return a + b
		if node.op == "-": return a - b
		if node.op == "*": return a * b
		if node.op == "/":
			if b == 0: raise EvalError("division by zero")
			return a / b
		if node.op == "^": return a ** b
		raise EvalError(f"unknown binary op {node.op}")
	if isinstance(node, Rel):
		cell = (ctx.focus[0] + node.dx, ctx.focus[1] + node.dy, ctx.focus[2] + node.dz)
		return cell_value(cell, ctx)
	if isinstance(node, Abs):
		return cell_value((node.x, node.y, node.z), ctx)
	raise EvalError(f"unknown node type {type(node).__name__}")

def cell_value(cell, ctx):
	# empty -> 0, numeric literal -> float, formula -> parse+eval, else EvalError.
	# cycles raise via ctx.visiting; successful values memoized in ctx.cache.
	if cell in ctx.visiting:
		raise EvalError(f"cycle through {cell}")
	if cell in ctx.cache:
		return ctx.cache[cell]
	raw = ctx.cells.get(cell, "")
	if not raw:
		ctx.cache[cell] = 0.
		return 0.
	if raw.startswith("="):
		node = parse(raw[1:])
		ctx.visiting.add(cell)
		try:
			sub = EvalCtx(focus=cell, cells=ctx.cells,
					visiting=ctx.visiting, cache=ctx.cache)
			v = evaluate(node, sub)
		finally:
			ctx.visiting.discard(cell)
		ctx.cache[cell] = v
		return v
	try:
		v = float(raw)
	except ValueError:
		raise EvalError(f"non-numeric {cell}: {raw!r}")
	ctx.cache[cell] = v
	return v

def parse_and_eval(text, focus_cell, cells, cache=None):
	# cache may be shared across calls to reuse resolved values; the caller
	# invalidates it when cell content changes.
	if cache is None: cache = {}
	if focus_cell in cache:
		return cache[focus_cell]
	ctx = EvalCtx(focus=focus_cell, cells=cells,
			visiting={focus_cell}, cache=cache)
	v = evaluate(parse(text), ctx)
	cache[focus_cell] = v
	return v
