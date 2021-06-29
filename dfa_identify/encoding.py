from __future__ import annotations

from itertools import product
from typing import Any, NamedTuple, Iterable, Literal, Optional

import attr
import funcy as fn
import networkx as nx
from networkx.algorithms.approximation.clique import max_clique
from datetime import datetime
from functools import partial

from dfa_identify.graphs import APTA, Node
import inspect

Nodes = Iterable[Node]
Clauses = Iterable[list[int]]

def validate_enc(func):
    def wrapper(self, *args):
        argnames = inspect.signature(func).parameters.keys()
        for i, argname in enumerate(argnames):
            if argname[:5] == "color":
                assert (args[i-1] < self.n_colors) and (args[i-1] >= 0), "color arg must be nonnegative and smaller than n_colors"
            elif argname[:4] == "node":
                assert (args[i-1] < self.n_nodes) and (args[i-1] >= 0), "node arg must be nonnegative and smaller than n_nodes"
            elif argname[:5] == "token":
                assert (args[i-1] < self.n_tokens) and (args[i-1] >= 0), "token arg must be nonnegative and smaller than n_tokens"
        return func(self, *args)
    return wrapper

# =================== Codec : int <-> variable  ====================


@attr.s(auto_detect=True, auto_attribs=True, frozen=True)
class ColorAcceptingVar:
    color: int
    true: bool


@attr.s(auto_detect=True, auto_attribs=True, frozen=True)
class ColorNodeVar:
    color: int
    true: bool
    node: int


@attr.s(auto_detect=True, auto_attribs=True, frozen=True)
class ParentRelationVar:
    parent_color: int
    node_color: int
    token: int
    true: bool


@attr.s(auto_detect=True, auto_attribs=True, frozen=True)
class Codec:
    n_nodes: int
    n_colors: int
    n_tokens: int
    symm_mode: Optional[Literal["clique", "bfs"]]

    counts: tuple[int] = attr.ib()
    @counts.default
    def counts_default(self):
        """
        Compute number of variables of each type.
        Used for calculating offsets for decoding and encoding
        Order: z, x, y, p, t, m
        """
        return [
            self.n_colors,
            self.n_colors * self.n_nodes,
            self.n_tokens * self.n_colors * self.n_colors,
            (self.n_colors * (self.n_colors - 1)) // 2,
            (self.n_colors * (self.n_colors - 1)) // 2,
            self.n_colors * self.n_tokens
            ]


    
    @staticmethod
    def from_apta(apta: APTA, n_colors: int = 0, symm_mode: Optional[Literal["clique", "bfs"]] = None) -> Codec:
        return Codec(len(apta.nodes), n_colors, len(apta.alphabet), symm_mode)

    @validate_enc
    def color_accepting(self, color: int) -> int:
        """ Literature refers to these variables as z """
        return sum(self.counts[:0]) + 1 + color

    @validate_enc
    def color_node(self, node: int, color: int) -> int:
        """ Literature refers to these variables as x """
        return sum(self.counts[:1]) + 1 + self.n_colors * node + color

    @validate_enc
    def parent_relation(self, token: Any, color1: int, color2: int) -> int:
        """ Literature refers to these variables as y """
        a = self.n_colors
        b = a**2
        return sum(self.counts[:2]) + 1 + color1 + a * color2 + b * token

    @validate_enc
    def enumeration_parent(self, color1: int, color2: int) -> int:
        """ Literature refers to these variables as p
        Note: here we use p_{i,j} rather than p_{j,i} """
        assert (color1 < color2), "color1 must be smaller than color2"
        return sum(self.counts[:3]) + 1 + (((color2) * (color2 - 1)) // 2) + color1

    @validate_enc
    def transition_relation(self, color1: int, color2: int) -> int:
        """ Literature refers to these variables as t """
        assert (color1 < color2), "color1 must be smaller than color2"
        return sum(self.counts[:4]) + 1 + (((color2) * (color2 - 1)) // 2) + color1

    @validate_enc
    def enumeration_label(self, token: Any, color: int) -> int:
        """ Literature refers to these variables as m """
        return sum(self.counts[:5]) + 1 + self.n_tokens * color + token


    def decode(self, lit: int) -> Var:
        idx = abs(lit) - 1
        color1, true = idx % self.n_colors, lit > 0
        kind_idx = idx // self.n_colors
        if kind_idx == 0:
            return ColorAcceptingVar(color1, true)
        elif 1 <= kind_idx <= self.n_nodes:
            node = (idx - color1) // self.n_colors - 1
            return ColorNodeVar(color1, true, node)
        tmp = idx - self.n_colors * (1 + self.n_nodes)
        tmp //= self.n_colors
        color2 = tmp % self.n_colors
        token = tmp // self.n_colors
        return ParentRelationVar(color1, color2, token, true)
            


# ================= Clause Generator =====================


def dfa_id_encodings(apta: APTA, symm_mode: Optional[Literal["clique", "bfs"]] = None) -> Iterable[Clauses]:
    cgraph = apta.consistency_graph()
    clique = max_clique(cgraph) if symm_mode == "clique" else None
    min_color = len(clique) if symm_mode == "clique" else 1

    for n_colors in range(min_color, len(apta.nodes) + 1):
        codec = Codec.from_apta(apta, n_colors, symm_mode = symm_mode)
        print("{} \tGenerating clauses for \t\tnodes = {} \tcolors = {}".format(datetime.now().strftime("%y%m%d-%H:%M:%S"),codec.n_nodes, codec.n_colors))
        yield codec, list(encode_dfa_id(apta, codec, cgraph, clique))

def encode_dfa_id(apta, codec, cgraph, clique = None):
    # Clauses from Table 1.                                      rows
    yield from onehot_color_clauses(codec)                     # 1, 5
    yield from partition_by_accepting_clauses(codec, apta)     # 2
    yield from colors_parent_rel_coupling_clauses(codec, apta) # 3, 7
    yield from onehot_parent_relation_clauses(codec)           # 4, 6
    yield from determination_conflicts(codec, cgraph)          # 8
    if codec.symm_mode == "clique":
        yield from symmetry_breaking(codec, clique)
    elif codec.symm_mode == "bfs":
        yield from symmetry_breaking_common(codec)
        yield from symmetry_breaking_bfs(codec)


def onehot_color_clauses(codec: Codec) -> Clauses:
    for n in range(codec.n_nodes):  # Each vertex has at least one color.
        yield [codec.color_node(n, c) for c in range(codec.n_colors)]

    for n in range(codec.n_nodes):  # Each vertex has at most one color.
        for i in range(codec.n_colors):
            lit = codec.color_node(n, i)
            for j in range(i + 1, codec.n_colors):  # i < j
                yield [-lit, -codec.color_node(n, j)]


def tokensXcolors(codec: Codec):
    return product(range(codec.n_tokens), range(codec.n_colors))

def onehot_parent_relation_clauses(codec: Codec) -> Clauses:
    # Each parent relation must target at least one color.
    for token, i in tokensXcolors(codec):
        colors = range(codec.n_colors)
        yield [codec.parent_relation(token, i, j) for j in colors]

    # Each parent relation can target at most one color.
    for token, i in tokensXcolors(codec):
        for h in range(codec.n_colors):
            lit1 = codec.parent_relation(token, i, h)
            for j in range(h + 1, codec.n_colors):  # h < j
                yield [-lit1, -codec.parent_relation(token, i, j)]


def partition_by_accepting_clauses(codec: Codec, apta: APTA) -> Clauses:
    for c in range(codec.n_colors):
        lit = codec.color_accepting(c)
        yield from ([-codec.color_node(n, c), lit] for n in apta.accepting)
        yield from ([-codec.color_node(n, c), -lit] for n in apta.rejecting)


def colors_parent_rel_coupling_clauses(codec: Codec, apta: APTA) -> Clauses:
    colors = range(codec.n_colors)
    rev_tree = apta.tree.reverse()
    non_root_nodes = set(apta.nodes) - {0}     # Root doesn't have a parent.
    for node, i, j in product(non_root_nodes, colors, colors):
        parent, *_ = rev_tree.neighbors(node)  # only have 1 parent.
        token = apta.alphabet[apta.nodes[node]['source']]

        parent_color = codec.color_node(parent, i)
        node_color = codec.color_node(node, j)
        parent_rel = codec.parent_relation(token, i, j)

        # Parent relation and node color coupled throuh parent color.
        yield [-parent_color, -node_color, parent_rel]  # 3
        yield [-parent_color, node_color, -parent_rel]  # 7


def determination_conflicts(codec: Codec, cgraph: nx.Graph) -> Clauses:
    colors = range(codec.n_colors)
    for (n1, n2), c in product(cgraph.edges, colors):
        yield [-codec.color_node(n1, c), -codec.color_node(n2, c)]


def symmetry_breaking(codec: Codec, clique: Nodes) -> Clauses:
    for color, node in enumerate(clique):
        yield [codec.color_node(node, color)]

def symmetry_breaking_common(codec: Codec) -> Clauses:
    """ 
    Symmetry breaking clauses for both DFS and BFS
    See Ulyantsev 2016 
    """
    yield [codec.color_node(0,0)] # Ensures start vertex is 0 - not listed in Ulyantsev
    for color2 in range(codec.n_colors):
        if color2 > 0:
            yield [codec.enumeration_parent(color1, color2) for color1 in range(color2)] # 4
        for color1 in range(color2):
            p = codec.enumeration_parent(color1, color2)
            t = codec.transition_relation(color1, color2)
            # m = partial(codec.enumeration_label, color2 = color2)
            # y = partial(codec.parent_relation, color1 = color1, color2 = color2)
            m = lambda l: codec.enumeration_label(l, color2)
            y = lambda l: codec.parent_relation(l, color1, color2)

            yield [-t] + [y(token) for token in range(codec.n_tokens)] # 1
            yield [t, -p] # 3

            for token2 in range(codec.n_tokens):
                yield [t, -y(token2)] # 2
                yield [-p, -m(token2), y(token2)] # 5
                yield [-y(token2), -p, m(token2)] + [y(token1) for token1 in range(token2)] # 7
                for token1 in range(token2):
                    yield [-p, -m(token2), -y(token1)] # 6

def symmetry_breaking_bfs(codec: Codec) -> Clauses:
    """ 
    Symmetry breaking clauses for BFS
    See Ulyantsev 2016 
    """
    for color2 in range(codec.n_colors):
        for color1 in range(color2):
            p = codec.enumeration_parent(color1, color2)
            t = codec.transition_relation(color1, color2)

            yield from [[-p, -codec.transition_relation(color3, color2)] for color3 in range(color1)] # 12
            yield [-t, p] + [codec.transition_relation(color3, color2) for color3 in range(color1)] # 13
            if color2 + 1 < codec.n_colors:
                yield from [[-p, -codec.enumeration_parent(color3, color2 + 1)] for color3 in range(color1)] # 14
                for token2 in range(codec.n_tokens):
                    for token1 in range(token2):
                        yield [-p, -codec.enumeration_parent(color1, color2 + 1), -codec.enumeration_label(token2, color2), -codec.enumeration_label(token1, color2 + 1)] # 15

__all__ = ['Codec', 'dfa_id_encodings']
