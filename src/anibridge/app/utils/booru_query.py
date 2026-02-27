"""Booru-like query parsing and evaluation.

This module defines a booru-like query language using pyparsing and provides helpers to
evaluate the parsed AST into a set of AniList IDs.

Supported syntax:
- Value search terms: `foo:bar` search for `bar` in field `foo`
- AniList search terms: `"foo"` search the AniList API for the bare term 'foo'
- AND: `foo bar` search for the intersection of both terms
- OR (prefix): `~foo ~bar baz` search for `(foo OR bar) AND baz` - tilde marks terms
    for OR grouping within AND
- OR (infix): `foo | bar baz` search for `foo OR (bar AND baz)` - pipe creates OR
    between AND expressions
- NOT: `-foo` search the negation of `foo`
- Grouping: `(foo | bar) baz` search for `(foo OR bar) AND baz`
- IN lists: `foo:bar,baz` search for foo matching any value in the list
- Ranges: `foo:<10 | foo:100..210` search for foo less than 10 or between 100 and 210
- Null literal: `foo:null` matches rows where `foo` is NULL
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import pyparsing as pp

from anibridge.app.exceptions import BooruQuerySyntaxError

__all__ = [
    "And",
    "BareTerm",
    "EvalResult",
    "KeyTerm",
    "Node",
    "Not",
    "Or",
    "collect_bare_terms",
    "collect_key_terms",
    "evaluate",
    "parse_query",
]

pp.ParserElement.enable_packrat()  # Supposed to speed up parsing

DbResolver = Callable[["KeyTerm"], set[int]]
AniListResolver = Callable[[str], list[int]]


class Node:
    """Base AST node for booru-like queries."""


@dataclass(frozen=True)
class ParsedValueToken:
    """Intermediate parsed representation of a key term value."""

    text: str
    quoted: bool


@dataclass
class KeyTerm(Node):
    """A key:value term that targets the local DB."""

    key: str
    value: str
    values: tuple[str, ...] | None = None
    quoted: bool = False


@dataclass
class BareTerm(Node):
    """A non-keyed term (word or phrase) that searches AniList."""

    text: str
    quoted: bool = False


@dataclass
class Not(Node):
    """Negation of a child expression."""

    child: Node
    _ids: set[int] | None = None  # Populated during evaluation


@dataclass
class And(Node):
    """Conjunction (implicit by whitespace)."""

    children: list[Node]


@dataclass
class Or(Node):
    """Disjunction (explicit with `|` or implicit with `~` prefix)."""

    children: list[Node]


@dataclass
class OrMarker(Node):
    """Marker for a term to be included in an OR group within an AND."""

    child: Node


def _make_parser() -> pp.ParserElement:
    identifier = pp.Word(pp.alphas, pp.alphanums + "_.")

    # Normalize identifier to lowercase
    identifier = identifier.set_parse_action(lambda _s, _loc, t: str(t[0]).lower())

    integer = pp.Word(pp.nums)
    restricted_chars = {"(", ")", "|", "~", '"'}
    word_chars = "".join(ch for ch in pp.printables if ch not in restricted_chars)
    word_token = pp.Word(word_chars)
    value_word_chars = "".join(
        ch for ch in pp.printables if ch not in (restricted_chars | {","})
    )
    value_word_token = pp.Word(value_word_chars)
    qstring_token = pp.QuotedString('"', esc_char="\\", unquote_results=True)

    # Tokens for comparisons and ranges
    cmp_op = pp.one_of("> >= < <=", caseless=False)
    cmp_val = pp.Combine(cmp_op + pp.Word(pp.nums))
    range_val = pp.Combine(pp.Word(pp.nums) + pp.Literal("..") + pp.Word(pp.nums))

    def _value_token(
        expr: pp.ParserElement, *, quoted: bool = False
    ) -> pp.ParserElement:
        """Wrap a value token parser to produce ParsedValueToken nodes."""
        return expr.copy().set_parse_action(
            lambda _s, _loc, toks: ParsedValueToken(
                text=str(toks[0]) if quoted else str(toks[0]).strip(),
                quoted=quoted,
            )
        )

    value_atom = (
        _value_token(qstring_token, quoted=True)
        | _value_token(range_val)
        | _value_token(cmp_val)
        | _value_token(value_word_token)
        | _value_token(integer)
    )

    value = pp.Group(pp.DelimitedList(value_atom, delim=","))

    colon = pp.Suppress(":")

    def _key_term_action(_s, _loc, toks):
        """Parse action to convert tokens into a KeyTerm node."""
        key = str(toks[0])
        raw_values = toks[1]
        tokens: list[ParsedValueToken] = []

        if isinstance(raw_values, ParsedValueToken):
            tokens = [raw_values]
        elif isinstance(raw_values, (pp.ParseResults, list)):
            tokens = [cast(ParsedValueToken, item) for item in raw_values]
        else:
            tokens = [ParsedValueToken(text=str(raw_values).strip(), quoted=False)]

        cleaned: list[ParsedValueToken] = [tok for tok in tokens if tok.text != ""]
        if not cleaned:
            return KeyTerm(key=key, value="", values=None, quoted=False)

        if len(cleaned) == 1:
            token = cleaned[0]
            return KeyTerm(
                key=key,
                value=token.text,
                values=None,
                quoted=token.quoted,
            )

        parts = tuple(dict.fromkeys(tok.text for tok in cleaned))
        return KeyTerm(key=key, value=",".join(parts), values=parts, quoted=False)

    key_term = (identifier + colon + value).set_parse_action(_key_term_action)

    # Normalize bare term to string
    bare_word = word_token.copy().set_parse_action(
        lambda _s, _loc, toks: BareTerm(text=str(toks[0]), quoted=False)
    )
    bare_qstring = qstring_token.copy().set_parse_action(
        lambda _s, _loc, toks: BareTerm(text=str(toks[0]), quoted=True)
    )
    bare = bare_qstring | bare_word

    # Define syntax grammar
    LPAR, RPAR = map(pp.Suppress, "()")
    expr: pp.Forward = pp.Forward()
    not_kw = pp.Keyword("not", caseless=True) | pp.Literal("-")
    tilde = pp.Literal("~")
    pipe = pp.Literal("|")
    atom = key_term | bare | pp.Group(LPAR + expr + RPAR)

    def _prefix_action(_s, _loc, toks):
        """Handle prefix operators.

        Tokens like ['~', '-', atom] or ['-', atom] or ['~', atom] or [atom]
        """
        if not toks:
            return toks
        parts = list(toks)
        node = parts[-1]
        if isinstance(node, list) and len(node) == 1 and isinstance(node[0], Node):
            node = node[0]
        for t in reversed(parts[:-1]):
            if str(t).lower() == "not" or str(t) == "-":
                node = Not(cast(Node, node))
            elif str(t) == "~":
                node = OrMarker(cast(Node, node))
        return node

    pref = (pp.ZeroOrMore(tilde | not_kw) + atom).set_parse_action(_prefix_action)

    def _and_action(_s, _loc, toks):
        """Handle conjunction of tokens."""
        required: list[Node] = []
        or_children: list[Node] = []
        for tok in toks:
            cur = tok
            if isinstance(cur, list) and len(cur) == 1 and isinstance(cur[0], Node):
                cur = cur[0]
            if isinstance(cur, OrMarker):
                or_children.append(cur.child)
            else:
                required.append(cast(Node, cur))

        nodes: list[Node] = []
        nodes.extend(required)
        if or_children:
            if len(or_children) == 1:
                nodes.append(or_children[0])
            else:
                nodes.append(Or(or_children))

        if not nodes:
            return And([])
        if len(nodes) == 1:
            return nodes[0]
        return And(nodes)

    conj = pp.OneOrMore(pref).set_parse_action(_and_action)

    def _or_action(_s, _loc, toks):
        """Handle disjunction of tokens separated by |."""
        nodes = [tok for tok in toks if tok != "|"]
        flattened = []
        for node in nodes:
            if isinstance(node, list) and len(node) == 1 and isinstance(node[0], Node):
                flattened.append(node[0])
            elif isinstance(node, Node):
                flattened.append(node)
            else:
                flattened.append(node)

        if len(flattened) == 1:
            return flattened[0]
        return Or(flattened)

    # OR has lower precedence than AND (conjunction)
    or_expr = (conj + pp.ZeroOrMore(pp.Suppress(pipe) + conj)).set_parse_action(
        _or_action
    )

    expr <<= or_expr
    return expr


PARSER = _make_parser()


def _merge_unquoted_bare_terms(node: Node) -> Node:
    """Group consecutive unquoted bare terms into single phrase terms."""
    if isinstance(node, BareTerm):
        return node

    if isinstance(node, Not):
        merged_child = _merge_unquoted_bare_terms(node.child)
        return Not(child=merged_child, _ids=node._ids)

    if isinstance(node, Or):
        merged_children = [_merge_unquoted_bare_terms(child) for child in node.children]
        return Or(merged_children)

    if isinstance(node, And):
        merged_children: list[Node] = []
        buffer: list[BareTerm] = []

        def _flush_buffer() -> None:
            """Flush buffered unquoted bare terms into a phrase term."""
            if not buffer:
                return
            if len(buffer) == 1:
                merged_children.append(buffer[0])
            else:
                phrase = " ".join(term.text for term in buffer)
                merged_children.append(BareTerm(text=phrase, quoted=False))
            buffer.clear()

        for child in node.children:
            merged_child = _merge_unquoted_bare_terms(child)
            if isinstance(merged_child, BareTerm) and not merged_child.quoted:
                buffer.append(merged_child)
                continue

            _flush_buffer()
            merged_children.append(merged_child)

        _flush_buffer()

        if not merged_children:
            return And([])
        if len(merged_children) == 1:
            return merged_children[0]
        return And(merged_children)

    return node


def parse_query(q: str) -> Node:
    """Parse the booru-like query string into an AST Node.

    Args:
        q (str): The booru-like query string to parse.

    Returns:
        Node: The root AST node representing the parsed query.

    Raises:
        pyparsing.ParseException on invalid input.
    """
    q = (q or "").strip()
    if not q:
        return And([])

    try:
        res = PARSER.parse_string(q, parse_all=True)
    except pp.ParseBaseException as exc:
        raise BooruQuerySyntaxError(str(exc)) from exc
    node_any = res[0]

    node: Node

    # Normalize single grouped result
    if (
        isinstance(node_any, list)
        and len(node_any) == 1
        and isinstance(node_any[0], Node)
    ):
        node = cast(Node, node_any[0])
    elif isinstance(node_any, Node):
        node = node_any
    elif isinstance(node_any, list) and node_any and isinstance(node_any[0], Node):
        node = cast(Node, node_any[0])
    else:
        raise BooruQuerySyntaxError("Invalid parsed AST for query")

    return _merge_unquoted_bare_terms(node)


@dataclass
class EvalResult:
    """Evaluation result for a query AST."""

    ids: set[int]
    order_hint: dict[int, int]
    used_bare: bool


def collect_bare_terms(node: Node) -> list[str]:
    """Collect bare term texts from the AST for prefetching.

    Args:
        node (Node): The root AST node.

    Returns:
        list[str]: A list with potential duplicates removed.
    """
    out: list[str] = []

    def _walk(n: Node) -> None:
        """Walk the AST and collect bare terms."""
        if isinstance(n, BareTerm):
            out.append(n.text)
            return
        if isinstance(n, pp.ParseResults):
            for child in n:
                _walk(cast(Node, child))
            return
        if isinstance(n, Not):
            _walk(n.child)
            return
        if isinstance(n, (And, Or)):
            for c in n.children:
                _walk(c)
            return

    _walk(node)

    # De-duplicate preserving first occurrence order
    seen: set[str] = set()
    unique: list[str] = []
    for t in out:
        if t not in seen:
            unique.append(t)
            seen.add(t)

    return unique


def collect_key_terms(node: Node) -> list[KeyTerm]:
    """Collect key:value terms from the AST for pre-resolution.

    Args:
        node (Node): The root AST node.

    Returns:
        list[KeyTerm]: Ordered list of encountered KeyTerm nodes.
    """
    out: list[KeyTerm] = []

    def _walk(n: Node) -> None:
        if isinstance(n, KeyTerm):
            out.append(n)
            return
        if isinstance(n, pp.ParseResults):
            for child in n:
                _walk(cast(Node, child))
            return
        if isinstance(n, Not):
            _walk(n.child)
            return
        if isinstance(n, (And, Or)):
            for child in n.children:
                _walk(child)

    _walk(node)
    return out


def evaluate(
    node: Node,
    *,
    db_resolver: DbResolver,
    anilist_resolver: AniListResolver,
    universe_ids: set[int] | None = None,
) -> EvalResult:
    """Evaluate AST into a set of AniList IDs with optional ordering hint.

    Args:
        node (Node): The root AST node to evaluate.
        db_resolver (DbResolver): Function to resolve KeyTerm nodes to AniList IDs.
        anilist_resolver (AniListResolver): Function to resolve BareTerm nodes to
            ordered AniList IDs.
        universe_ids (set[int] | None): Optional set of AniList IDs to use as
            universe for NOT operations. If None, universe is derived from
            all IDs seen in positive terms.

    Returns:
        EvalResult: Evaluation result containing:
            - ids: Set of AniList IDs matching the query.
            - order_hint: Maps AniList ID to rank (lower is earlier) derived from
                BareTerm resolution order.
            - used_bare: True if any BareTerm was used in the query.
    """
    used_bare = False
    order_hint: dict[int, int] = {}
    universe: set[int] = set(universe_ids or set())

    def _coerce(n_any) -> Node | Any:
        """Unwrap pyparsing Group/ParseResults that contain a single Node.

        This occurs for parenthesized expressions like -(foo | bar), where the
        grouped child may arrive as a ParseResults([Node]).
        """
        try:
            if isinstance(n_any, (list, pp.ParseResults)) and len(n_any) == 1:
                return _coerce(n_any[0])
        except Exception:
            pass
        return n_any

    def eval_node(n: Node | Any) -> set[int]:
        nonlocal used_bare, order_hint, universe
        n = _coerce(n)
        if isinstance(n, And):
            if not n.children:
                # Empty AND, return Universe
                return set(universe)
            acc: set[int] | None = None
            for c in n.children:
                s = eval_node(c)
                if acc is None:
                    acc = set(s)
                else:
                    acc &= s
                if not acc:
                    # Early exit on empty intersection
                    return set()
            return acc or set()
        if isinstance(n, Or):
            out: set[int] = set()
            for c in n.children:
                out |= eval_node(c)
            return out
        if isinstance(n, Not):
            # Local complement relative to the universe
            child = eval_node(n.child)
            return set(universe) - set(child)
        if isinstance(n, KeyTerm):
            return set(db_resolver(n))
        if isinstance(n, BareTerm):
            used_bare = True
            ordered = anilist_resolver(n.text)
            for idx, aid in enumerate(ordered):
                prev = order_hint.get(aid, idx)
                order_hint[aid] = prev if prev <= idx else idx
            return set(ordered)
        return set()

    ids = eval_node(node)

    return EvalResult(ids=ids, order_hint=order_hint, used_bare=used_bare)
