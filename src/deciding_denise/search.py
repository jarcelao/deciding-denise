"""Fast duel search: worst-case minimax over simultaneous moves with a territory evaluation.

Rules mirror engine.simulate (standard, non-wrapped, no future food spawns) but run on
plain tuples so a few plies fit in a turn.
"""

from collections import namedtuple
from time import monotonic

WIN, LOSS, DRAW = 1000, -1000, -100
STEPS = {"up": (0, 1), "right": (1, 0), "down": (0, -1), "left": (-1, 0)}


class Board:
    def __init__(self, width, height, hazards, damage):
        self.width, self.height = width, height
        self.hazards, self.damage = hazards, damage
        # bit y * width + x; the column masks stop shifts wrapping across rows
        self.full = (1 << (width * height)) - 1
        self.not_first_col = sum(
            1 << (y * width + x) for y in range(height) for x in range(1, width)
        )
        self.not_last_col = sum(
            1 << (y * width + x) for y in range(height) for x in range(width - 1)
        )

    def spread(self, bits):
        """Cells one step from any cell in bits (inside the board)."""
        w = self.width
        return (
            (bits << 1 & self.not_first_col)
            | (bits >> 1 & self.not_last_col)
            | (bits << w & self.full)
            | bits >> w
        )

    def inside(self, c):
        return 0 <= c[0] < self.width and 0 <= c[1] < self.height


# Both snakes plus remaining food; bodies are head-first tuples.
Node = namedtuple("Node", "mine mine_hp theirs theirs_hp food")


def free_times(*bodies):
    """Cell -> first turn it can be entered (a body cell frees as the tail passes)."""
    out = {}
    for body in bodies:
        n = len(body)
        for k, cell in enumerate(body):
            out[cell] = max(out.get(cell, 0), n - k)
    return out


def legal_moves(board, body, blocked):
    hx, hy = body[0]
    out = []
    for name, (dx, dy) in STEPS.items():
        dest = (hx + dx, hy + dy)
        if board.inside(dest) and blocked.get(dest, 0) <= 1:
            out.append(name)
    return out or list(STEPS)


def advance(board, body, hp, move, food):
    dx, dy = STEPS[move]
    head = (body[0][0] + dx, body[0][1] + dy)
    ate = head in food
    new = (head, *body[:-1])
    if ate:
        new += (new[-1],)
    hp = 100 if ate else hp - 1 - (board.damage if head in board.hazards else 0)
    return new, hp, ate


def step(board, node, my_move, their_move):
    """Return (result, None) if the game ended (WIN/LOSS/DRAW) else (None, next node)."""
    mine, mine_hp, ate1 = advance(board, node.mine, node.mine_hp, my_move, node.food)
    theirs, theirs_hp, ate2 = advance(
        board, node.theirs, node.theirs_hp, their_move, node.food
    )
    alive1 = mine_hp > 0 and board.inside(mine[0])
    alive2 = theirs_hp > 0 and board.inside(theirs[0])
    dead1 = not alive1 or (
        mine[0] in mine[1:]
        or (alive2 and mine[0] in theirs[1:])
        or (alive2 and mine[0] == theirs[0] and len(mine) <= len(theirs))
    )
    dead2 = not alive2 or (
        theirs[0] in theirs[1:]
        or (alive1 and theirs[0] in mine[1:])
        or (alive1 and mine[0] == theirs[0] and len(theirs) <= len(mine))
    )
    if dead1 or dead2:
        return (DRAW if dead1 and dead2 else LOSS if dead1 else WIN), None
    eaten = {head for head, ate in ((mine[0], ate1), (theirs[0], ate2)) if ate}
    return None, Node(mine, mine_hp, theirs, theirs_hp, node.food - eaten)


def territory(board, node):
    """(cells I reach first, cells they reach first, foods I reach first, foods they do).

    Bitboard flood fill from both heads; a body cell opens once its tail has passed and a
    cell reached on the same turn goes to the longer snake (to them when equal).
    """
    w = board.width
    bit = lambda c: 1 << (c[1] * w + c[0])
    solid, opens = 0, {}
    for body in (node.mine, node.theirs):
        n = len(body)
        for k, cell in enumerate(body):
            b = bit(cell)
            if not solid & b:  # head-most occurrence frees last
                solid |= b
                opens[n - k] = opens.get(n - k, 0) | b
    mine = seen_m = own_m = bit(node.mine[0])
    theirs = seen_t = own_t = bit(node.theirs[0])
    longer = len(node.mine) > len(node.theirs)
    for t in range(1, board.width * board.height):
        solid &= ~opens.get(t, 0)
        mine, theirs = (
            board.spread(mine) & ~seen_m & ~solid,
            board.spread(theirs) & ~seen_t & ~solid,
        )
        if not mine and not theirs:
            break
        tie = mine & theirs
        own_m |= mine & ~seen_t & ~tie | (tie if longer else 0)
        own_t |= theirs & ~seen_m & ~tie | (0 if longer else tie)
        seen_m |= mine
        seen_t |= theirs
    food = sum(map(bit, node.food))
    return (
        own_m.bit_count(),
        own_t.bit_count(),
        (own_m & food).bit_count(),
        (own_t & food).bit_count(),
    )


def evaluate(board, node):
    my_cells, their_cells, my_food, their_food = territory(board, node)
    score = my_cells - their_cells + 3 * (len(node.mine) - len(node.theirs))
    score += 4 * (my_food - their_food)
    if my_cells < len(node.mine):
        score -= 30 + 5 * (len(node.mine) - my_cells)
    if their_cells < len(node.theirs):
        score += 30 + 5 * (len(node.theirs) - their_cells)
    if node.mine_hp < 40:
        score -= (40 - node.mine_hp) * (1 if my_food else 3)
    # stay strictly between the forced win/loss bands so a heuristic never reads as proof
    return max(LOSS + 101, min(WIN - 101, score))


def terminal(result, ply):
    """Prefer faster wins and slower losses."""
    return result - ply if result > 0 else result + ply


def value(board, node, depth, alpha, beta, deadline, ply=1):
    """Worst-case value for me: I pick the max over the enemy's best reply (min)."""
    if depth == 0:
        return evaluate(board, node)
    if deadline is not None and monotonic() > deadline:
        raise TimeoutError
    blocked = free_times(node.mine, node.theirs)
    their_moves = legal_moves(board, node.theirs, blocked)
    best = -(10**6)
    for mine in legal_moves(board, node.mine, blocked):
        worst = 10**6
        for theirs in their_moves:
            result, nxt = step(board, node, mine, theirs)
            if result is None:
                v = value(
                    board,
                    nxt,
                    depth - 1,
                    max(alpha, best),
                    min(beta, worst),
                    deadline,
                    ply + 1,
                )
            else:
                v = terminal(result, ply)
            worst = min(worst, v)
            if worst <= max(alpha, best):
                break
        best = max(best, worst)
        if best >= beta:
            break
    return best


def build(state):
    """Board and root Node from a Battlesnake state (duel, us first)."""
    b = state["board"]
    you = state["you"]["id"]
    snakes = {s["id"]: s for s in b["snakes"]}
    me = snakes[you]
    them = next(s for sid, s in snakes.items() if sid != you)
    body = lambda s: tuple((p["x"], p["y"]) for p in s["body"])
    damage = state["game"]["ruleset"].get("settings", {}).get("hazardDamagePerTurn", 14)
    board = Board(
        b["width"],
        b["height"],
        {(p["x"], p["y"]) for p in b.get("hazards", [])},
        damage,
    )
    node = Node(
        body(me),
        me["health"],
        body(them),
        them["health"],
        frozenset((p["x"], p["y"]) for p in b["food"]),
    )
    return board, node


def root_values(state, max_depth, deadline):
    """{my move: {their reply: value}} at the deepest fully searched depth, plus that depth."""
    board, node = build(state)
    result, depth_done = {}, 0
    for depth in range(1, max_depth + 1):
        table = {}
        try:
            for mine in STEPS:
                table[mine] = {}
                for theirs in STEPS:
                    res, nxt = step(board, node, mine, theirs)
                    table[mine][theirs] = (
                        terminal(res, 1)
                        if res is not None
                        else value(board, nxt, depth - 1, -(10**6), 10**6, deadline, 2)
                    )
        except TimeoutError:
            break
        result, depth_done = table, depth
        scores = [min(replies.values()) for replies in table.values()]
        # a forced win, or at most one move left that doesn't lose, is settled for good
        if max(scores) >= WIN - 100 or sum(v > LOSS + 100 for v in scores) <= 1:
            break
    return result, depth_done
