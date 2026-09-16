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
    if right[0] == "union" and left in right[1]:
        return EMPTY
    if left[0] == "union":
        return union(*(difference(part, right) for part in left[1]))
    if right[0] == "union":
        right = union(*(part for part in right[1] if not disjoint_prefixes(left, part)))
    elif disjoint_prefixes(left, right):
        return left
    return left if right == EMPTY else ("difference", left, right)


def intersection(left, right):
    if left == EMPTY or right == EMPTY or disjoint_prefixes(left, right):
        return EMPTY
    if left == right:
        return left
    if left[0] == "union":
        return union(*(intersection(part, right) for part in left[1]))
    if right[0] == "union":
        return union(*(intersection(left, part) for part in right[1]))
    return ("intersection", left, right)


@lru_cache(maxsize=32768)
def literal_prefix(item):
    """Return a mandatory byte prefix and whether it is the whole expression."""
    op = item[0]
    if op == "one":
        return b"", True
    if op == "chars" and len(item[1]) == 1:
        return bytes(item[1]), True
    if op == "seq":
        prefix = b""
        for part in item[1]:
            value, complete = literal_prefix(part)
            prefix += value
            if not complete:
                return prefix, False
        return prefix, True
    if op == "difference":
        return literal_prefix(item[1])[0], False
    return b"", False


def disjoint_prefixes(left, right):
    a, _ = literal_prefix(left)
    b, _ = literal_prefix(right)
    return not (a.startswith(b) or b.startswith(a))


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
    if op == "intersection":
        return nullable(item[1]) and nullable(item[2])
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
    if op == "intersection":
        return intersection(derivative(item[1], byte), derivative(item[2], byte))
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
        elif part[0] in ("star", "difference", "intersection"):
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


def eliminate(item):
    """Eliminate DFA states, returning an ordinary regex expression."""
    alphabet, edges, accepting = dfa(item)
    edges, accepting, initial = minimize(edges, accepting)
    useful = set(accepting)
    while True:
        expanded = useful | {i for i, row in enumerate(edges) if any(j in useful for j in row)}
        if expanded == useful:
            break
        useful = expanded
    if initial not in useful:
        return EMPTY
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
    return graph[start, end]


@lru_cache(maxsize=4096)
def lower(item, *, valid_paths=False):
    """Preserve ordinary regex operations; eliminate only unresolved set operations."""
    # Only simplify the original component expressions at path boundaries, never
    # expressions reconstructed by state elimination inside a larger component.
    if valid_paths and item == COMPONENT:
        return seq(COMPONENT_CHAR, star(COMPONENT_CHAR))
    op = item[0]
    if op in ("difference", "intersection", "machine"):
        return eliminate(compiled(item))
    if op == "union":
        return union(*(lower(part, valid_paths=valid_paths) for part in item[1]))
    if op == "seq":
        return seq(*(lower(part, valid_paths=valid_paths) for part in item[1]))
    if op == "star":
        return star(lower(item[1], valid_paths=valid_paths))
    return item


@lru_cache(maxsize=4096)
def without_epsilon(item):
    """Remove the empty string using ordinary regex operations only."""
    if not nullable(item):
        return item
    if item == EPSILON:
        return EMPTY
    if item[0] == "union":
        return union(*(without_epsilon(part) for part in item[1]))
    if item[0] == "star":
        return seq(without_epsilon(item[1]), item)
    if item[0] == "seq":
        return union(
            *(seq(without_epsilon(part), *item[1][i + 1 :]) for i, part in enumerate(item[1]))
        )
    raise AssertionError("Set operations must be lowered before removing the empty string")


def regex(item, *, valid_paths=False):
    item = lower(item, valid_paths=valid_paths)
    if valid_paths:
        # Unison also tests the empty path representing the synchronization root.
        # A nullable glob must not prune that root and hide its included children.
        item = without_epsilon(item)
    return None if item == EMPTY else render(item)


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
NON_DOT = chars(ALPHABET - {ord("/"), ord(".")})
COMPONENT = union(
    seq(NON_DOT, star(COMPONENT_CHAR)),
    seq(literal("."), NON_DOT, star(COMPONENT_CHAR)),
    seq(literal(".."), COMPONENT_CHAR, star(COMPONENT_CHAR)),
)
SLASH = literal("/")
PATHS = seq(COMPONENT, star(seq(SLASH, COMPONENT)))


def descendants(item):
    if item[0] == "union":
        return union(*(descendants(part) for part in item[1]))
    return seq(item, SLASH, PATHS)
