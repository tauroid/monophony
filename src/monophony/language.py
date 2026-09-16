"""Byte-string regular languages and bounded, symbolic decision procedures.

Union, concatenation, Kleene star and difference form the IR. Derivatives
produce a DFA for emptiness/witness checks and ordinary-regex lowering.
No filesystem entries are used to construct these languages.
"""

from collections import deque
from dataclasses import dataclass
from functools import lru_cache

from .errors import TranslationError

EMPTY = ("zero",)
EPSILON = ("one",)
ALPHABET = frozenset(range(1, 256))  # NUL cannot occur in a filename.
MAX_STATES = 1024
MAX_INTERMEDIATE_STATES = 8192
MAX_REGEX = 24_000


@dataclass(frozen=True, eq=False)
class Machine:
    alphabet: tuple
    edges: tuple
    accepting: frozenset
    initial: int


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


def compiled(item, *, intermediate_limit=MAX_INTERMEDIATE_STATES):
    if item == EMPTY or item[0] == "machine":
        return item
    alphabet, edges, accepting = dfa(item, max_states=intermediate_limit)
    edges, accepting, initial = minimize(edges, accepting)
    if not accepting:
        return EMPTY
    if len(edges) > MAX_STATES:
        raise TranslationError(f"Path matcher exceeds {MAX_STATES} minimized states")
    machine = Machine(tuple(alphabet), tuple(map(tuple, edges)), frozenset(accepting), initial)
    return ("machine", machine, initial)


@lru_cache(maxsize=4096)
def combine(left, right, *, subtract=False):
    """Combine minimized matchers, minimizing after each ordered rule."""
    if right == EMPTY:
        return left
    if left == EMPTY:
        return EMPTY if subtract else right
    left_machine, left_start = left[1:]
    right_machine, right_start = right[1:]
    alphabet = []
    columns = []
    for i, left_group in enumerate(left_machine.alphabet):
        for j, right_group in enumerate(right_machine.alphabet):
            group = left_group & right_group
            if group:
                alphabet.append(group)
                columns.append((i, j))
    states = [(left_start, right_start)]
    indices = {states[0]: 0}
    edges = []
    accepting = set()
    for pair in states:
        a, b = pair
        accepted = (
            (a in left_machine.accepting and b not in right_machine.accepting)
            if subtract
            else (a in left_machine.accepting or b in right_machine.accepting)
        )
        if accepted:
            accepting.add(indices[pair])
        row = []
        for i, j in columns:
            destination = (left_machine.edges[a][i], right_machine.edges[b][j])
            if destination not in indices:
                if len(states) >= MAX_INTERMEDIATE_STATES:
                    raise TranslationError(
                        "Cannot convert .gitignore rules: combining path matchers "
                        f"exceeds {MAX_INTERMEDIATE_STATES} intermediate states"
                    )
                indices[destination] = len(states)
                states.append(destination)
            row.append(indices[destination])
        edges.append(row)
    edges, accepting, initial = minimize(edges, accepting)
    if not accepting:
        return EMPTY
    if len(edges) > MAX_STATES:
        raise TranslationError(f"Path matcher exceeds {MAX_STATES} minimized states")
    machine = Machine(tuple(alphabet), tuple(map(tuple, edges)), frozenset(accepting), initial)
    return ("machine", machine, initial)


@lru_cache(maxsize=32768)
def nullable(item):
    op = item[0]
    if op in ("one", "star"):
        return True
    if op in ("zero", "chars"):
        return False
    if op == "machine":
        return item[2] in item[1].accepting
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
    if op == "machine":
        machine = item[1]
        column = next(i for i, group in enumerate(machine.alphabet) if byte in group)
        return ("machine", machine, machine.edges[item[2]][column])
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
        elif part[0] == "machine":
            predicates.update(part[1].alphabet)
        elif part[0] in ("seq", "union"):
            pending.extend(part[1])
        elif part[0] in ("star", "difference"):
            pending.extend(part[1:])
    groups = [ALPHABET]
    for predicate in predicates:
        groups = [p for group in groups for p in (group & predicate, group - predicate) if p]
    return sorted(groups, key=min)


def dfa(item, *, max_states=None):
    if max_states is None:
        max_states = MAX_STATES
    alphabet = partitions(item)
    states = [item]
    indices = {item: 0}
    edges = []
    for state in states:
        row = []
        for group in alphabet:
            dest = derivative(state, min(group))
            if dest not in indices:
                if len(states) >= max_states:
                    raise TranslationError(
                        "Cannot convert .gitignore rules: the path-matching automaton "
                        f"exceeds {max_states} intermediate states"
                    )
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
        incoming_counts = {k: 0 for k in useful}
        outgoing_counts = {k: 0 for k in useful}
        for a, b in graph:
            if a != b:
                if b in incoming_counts:
                    incoming_counts[b] += 1
                if a in outgoing_counts:
                    outgoing_counts[a] += 1
        k = min(useful, key=lambda k: (incoming_counts[k] * outgoing_counts[k], k))
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
