"""Byte-string regular languages and bounded, symbolic decision procedures.

Union, concatenation, Kleene star and difference form the IR. Derivatives
produce a DFA for emptiness/witness checks and ordinary-regex lowering.
No filesystem entries are used to construct these languages.
"""

from collections import deque
from functools import lru_cache

from .errors import TranslationError

EMPTY = ("zero",)
EPSILON = ("one",)
ALPHABET = frozenset(range(1, 256))  # NUL cannot occur in a filename.
MAX_STATES = 256
MAX_REGEX = 24_000


def chars(values):
    values = frozenset(values)
    return ("chars", values) if values else EMPTY


def literal(value: str):
    if not value.isascii() or "\0" in value:
        raise TranslationError("Non-ASCII patterns/scope names are not yet supported")
    return seq(*(chars([ord(c)]) for c in value))


def union(*items):
    values = set()
    for item in items:
        if item != EMPTY:
            values.update(item[1] if item[0] == "union" else [item])
    if not values:
        return EMPTY
    if len(values) == 1:
        return next(iter(values))
    return ("union", frozenset(values))


def seq(*items):
    values = []
    for item in items:
        if item == EMPTY:
            return EMPTY
        if item != EPSILON:
            values.extend(item[1] if item[0] == "seq" else [item])
    return ("seq", tuple(values)) if len(values) > 1 else values[0] if values else EPSILON


def star(item):
    return EPSILON if item in (EMPTY, EPSILON) else item if item[0] == "star" else ("star", item)


def difference(left, right):
    if left == right or left == EMPTY:
        return EMPTY
    return left if right == EMPTY else ("difference", left, right)


def intersection(left, right):
    return difference(left, difference(left, right))


@lru_cache(maxsize=32768)
def nullable(item):
    op = item[0]
    if op in ("one", "star"):
        return True
    if op in ("zero", "chars"):
        return False
    if op == "union":
        return any(map(nullable, item[1]))
    if op == "seq":
        return all(map(nullable, item[1]))
    return nullable(item[1]) and not nullable(item[2])


@lru_cache(maxsize=65536)
def derivative(item, byte):
    op = item[0]
    if op in ("zero", "one"):
        return EMPTY
    if op == "chars":
        return EPSILON if byte in item[1] else EMPTY
    if op == "union":
        return union(*(derivative(x, byte) for x in item[1]))
    if op == "difference":
        return difference(derivative(item[1], byte), derivative(item[2], byte))
    if op == "star":
        return seq(derivative(item[1], byte), item)
    terms = []
    for i, part in enumerate(item[1]):
        terms.append(seq(derivative(part, byte), *item[1][i + 1 :]))
        if not nullable(part):
            break
    return union(*terms)


def matches(item, path: str):
    for byte in path.encode("utf-8", "surrogateescape"):
        item = derivative(item, byte)
    return nullable(item)


def partitions(item):
    """Only distinguish bytes that some character predicate distinguishes."""
    predicates = set()
    pending = [item]
    seen = set()
    while pending:
        part = pending.pop()
        if part in seen:
            continue
        seen.add(part)
        if part[0] == "chars":
            predicates.add(part[1])
        elif part[0] in ("seq", "union"):
            pending.extend(part[1])
        elif part[0] in ("star", "difference"):
            pending.extend(part[1:])
    groups = [ALPHABET]
    for predicate in predicates:
        groups = [p for group in groups for p in (group & predicate, group - predicate) if p]
    return sorted(groups, key=min)


def dfa(item):
    alphabet = partitions(item)
    states = [item]
    indices = {item: 0}
    edges = []
    for state in states:
        row = []
        for group in alphabet:
            dest = derivative(state, min(group))
            if dest not in indices:
                if len(states) >= MAX_STATES:
                    raise TranslationError(f"Language exceeds {MAX_STATES} states")
                indices[dest] = len(states)
                states.append(dest)
            row.append(indices[dest])
        edges.append(row)
    return alphabet, edges, {i for i, state in enumerate(states) if nullable(state)}


def witness(item):
    alphabet, edges, accepting = dfa(item)
    queue = deque([(0, b"")])
    seen = {0}

    def for_state(group):
        return next((x for x in b"a0_./" if x in group), min(group))

    while queue:
        state, path = queue.popleft()
        if state in accepting:
            return path.decode("utf-8", "backslashreplace")
        for group, dest in zip(alphabet, edges[state], strict=True):
            if dest not in seen:
                seen.add(dest)
                queue.append((dest, path + bytes([for_state(group)])))
    return None


def minimize(edges, accepting):
    groups = [int(i in accepting) for i in range(len(edges))]
    while True:
        labels = {}
        updated = []
        for i, row in enumerate(edges):
            signature = (i in accepting, tuple(groups[j] for j in row))
            updated.append(labels.setdefault(signature, len(labels)))
        if updated == groups:
            break
        groups = updated
    rows = [None] * len(set(groups))
    for i, group in enumerate(groups):
        rows[group] = [groups[j] for j in edges[i]]
    return rows, {groups[i] for i in accepting}, groups[0]


def regex(item):
    """Eliminate DFA states; return None for the empty language."""
    alphabet, edges, accepting = dfa(item)
    edges, accepting, initial = minimize(edges, accepting)
    useful = set(accepting)
    while True:
        expanded = useful | {i for i, row in enumerate(edges) if any(j in useful for j in row)}
        if expanded == useful:
            break
        useful = expanded
    if initial not in useful:
        return None
    start, end = len(edges), len(edges) + 1
    graph = {(start, initial): EPSILON}
    for i in sorted(useful):
        if i in accepting:
            graph[i, end] = EPSILON
        by_dest = {}
        for group, dest in zip(alphabet, edges[i], strict=True):
            if dest in useful:
                by_dest.setdefault(dest, set()).update(group)
        for dest, values in by_dest.items():
            graph[i, dest] = chars(values)
    while useful:

        def cost(k, graph=graph):
            return sum(b == k and a != k for a, b in graph) * sum(
                a == k and b != k for a, b in graph
            )

        k = min(useful, key=lambda k: (cost(k), k))
        incoming = [(a, r) for (a, b), r in graph.items() if b == k and a != k]
        outgoing = [(b, r) for (a, b), r in graph.items() if a == k and b != k]
        loop = star(graph.get((k, k), EMPTY))
        for a, left in incoming:
            for b, right in outgoing:
                result = union(graph.get((a, b), EMPTY), seq(left, loop, right))
                render(result)  # Enforce output limits during elimination as well.
                graph[a, b] = result
        graph = {key: value for key, value in graph.items() if k not in key}
        useful.remove(k)
    return render(graph[start, end])


def render(item):
    def emit(item):
        op = item[0]
        if op == "one":
            return "a{0}"
        if op == "chars":
            values = item[1]
            if len(values) == 1:
                c = chr(next(iter(values)))
                if c == " ":
                    return "[ ]"
                return "\\" + c if c in "|()*+?[.^${\\" else c
            negative = 128 in values
            values = ALPHABET - values if negative else values
            if any(x >= 128 for x in values):
                raise TranslationError("Cannot emit a non-ASCII byte class")

            def char(x):
                c = chr(x)
                return "[." + c + ".]" if c in "[]-^" else c

            if not values:
                return "."
            return "[" + ("^" if negative else "") + "".join(char(x) for x in sorted(values)) + "]"
        if op == "seq":
            result = "".join(emit(x) for x in item[1])
        elif op == "union":
            result = "(" + "|".join(sorted(emit(x) for x in item[1])) + ")"
        elif op == "star":
            result = "(" + emit(item[1]) + ")*"
        else:
            raise AssertionError("Set operations must be eliminated before rendering")
        if len(result) > MAX_REGEX:
            raise TranslationError(f"Generated regex exceeds {MAX_REGEX} characters")
        return result

    return emit(item)


COMPONENT_CHAR = chars(ALPHABET - {ord("/")})
COMPONENT = difference(
    seq(COMPONENT_CHAR, star(COMPONENT_CHAR)), union(literal("."), literal(".."))
)
SLASH = literal("/")
PATHS = seq(COMPONENT, star(seq(SLASH, COMPONENT)))


def descendants(item):
    return seq(item, SLASH, PATHS)
