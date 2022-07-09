import tensorflow as tf
import numpy as np
import itertools
import re
import util
from parse import BCParser
from ast_nodes import EinTup, IntExpr, Dims, ArithmeticBinOp, StaticExpr

class Runtime(object):
    def __init__(self, min_dim=1, max_dim=100):
        self.parser = BCParser() 
        # map of eintup names to EinTup instances
        self.tups = {}

        # defines the signature (see notes.txt) of arrays
        self.array_sig = {}

        # stores current values of the arrays.  the shape
        # either matches or is broadcastable to the signature
        self.arrays = {}

        # The program statement top-level AST nodes
        self.statements = None 

        # Ast nodes representing rank and dim comstraints
        self.constraints = None

        self.min_dim = IntExpr(self, min_dim)
        self.max_dim = IntExpr(self, max_dim) 
        self.parser.set_runtime(self)

    def __repr__(self):
        tups = 'Tups: \n' + '\n'.join(repr(tup) for tup in self.tups.values())

        sigs = 'Array Signatures: \n' 
        sigs += '\n'.join(name + ': ' + repr(sig) for name, sig in
                self.array_sig.items())

        shapes = 'Array Shapes: \n'
        shapes += '\n'.join(name + ': ' + repr(ary.shape) 
                for name, ary in self.arrays.items())

        statements = 'Statements: \n'
        statements += '\n'.join(repr(st) for st in self.statements)

        constraints = 'Constraints: \n'
        constraints += '\n'.join(repr(c) for c in self.dims_constraints)
        constraints += '\n'.join(repr(c) for c in self.rank_constraints)

        tfcall = 'TF Call: \n'
        tfcall += repr(self.tf_call)

        out_args = 'Output Args: \n'
        out_args += repr(self.out_args)

        return (f'{tups}\n\n{sigs}\n\n{shapes}\n\n{statements}\n\n'
                f'{tfcall}\n\n{out_args}\n')

    def parse_et_file(self, et_file):
        with open(et_file, 'r') as fh:
            content = fh.read()

        sections = iter(re.split('\n\n+', content.strip()))
        statements = next(sections)
        tf_call = next(sections)
        tf_output = next(sections)
        constraints = next(sections, '')

        statements = statements.strip().split('\n')
        tf_call = tf_call.replace('\n', ' ').strip()
        tf_output = tf_output.replace('\n', ' ').strip()
        constraints = constraints.strip().split('\n')

        self.parser.set_statement_mode()
        self.statements = [ self.parser.parse(st) for st in statements ]

        self.parser.set_tfcall_mode()
        self.tf_call = self.parser.parse(tf_call)

        # ordered list of TensorArg nodes in the order matching expected tf
        # output
        self.parser.set_output_mode()
        self.out_args = self.parser.parse(tf_output)
        
        self.parser.set_constraint_mode()
        for con in constraints:
            self.parser.parse(con)

        # post-init all AST nodes
        all_nodes = self.statements + [self.tf_call]

        for node in all_nodes: 
            node.post_parse_init()

        # self.register_dims_limits()

    def register_dims_limits(self):
        # add Dims constraints to appropriate EinTups
        def plus1(expr):
            return ArithmeticBinOp(expr, IntExpr(self, 1), '+')

        all_ops = ['<','<=','==','>=','>']
        for con in self.dims_constraints:
            flipped_op = dict(zip(all_ops, reversed(all_ops)))[con.op_string]
            g1 = (con.op_string, con.arg1, con.arg2)
            g2 = (flipped_op, con.arg2, con.arg1)
            for op, lhs, rhs in g1, g2:
                min_expr = max_expr = None
                if isinstance(lhs, Dims):
                    if op == '<':
                        max_expr = rhs
                    elif op == '<=':
                        max_expr = plus1(rhs)
                    elif op == '==':
                        min_expr = rhs 
                        max_expr = plus1(rhs) 
                    elif op == '>=':
                        min_expr = rhs 
                    elif op == '>':
                        min_expr = plus1(rhs) 
                if min_expr is not None:
                    for tup in lhs.base_tups:
                        tup.maybe_add_min_expr(min_expr)
                if max_expr is not None:
                    for tup in lhs.base_tups:
                        tup.maybe_add_max_expr(max_expr)
        
    def clear_shapes(self):
        for tup in self.tups.values():
            tup.clear()

    # run the full program and produce the set of output tensors in the
    # preconfigured order
    def run(self):
        if not all(con.value() for con in self.constraints):
            return None
        for st in self.statements:
            st.evaluate()
        outs = { (arg.name, arg.value()) for arg in self.outputs }
        return outs

    def gen_dims(self):
        for tup in self.tups.values():
            if not tup.primary() or tup.has_dims():
                continue
            tup.gen_dims()

    # cycle through all combinations of ranks < 10 satisfying constraints
    """
    def cycle(self, k):
        cons = self.rank_constraints
        if k == -1:
            yield 
            return
        pre_tups = set(tup.name for con in cons[:k] for tup in con.get_tups())
        cur_tups = set(tup.name for tup in cons[k].get_tups())
        extra = list(cur_tups.difference(pre_tups))
        for _ in self.cycle(k-1):
            for cur_ranks in np.ndindex((10,) * len(extra)):
                update = dict(zip(extra, cur_ranks))
                self.set_ranks(update)
                if cons[k].value():
                    yield
    """

    def validate_all(self):
        te = { t: t.rank_expr for t in self.tups.values() }
        rng = { k: v for k, v in te.items() if isinstance(v, tuple) }
        expr_tups = [ k for k, v in te.items() if isinstance(v, StaticExpr) ]
        range_list = [range(r[0], r[1]+1) for r in rng.values()]
        combos = itertools.product(*range_list)
        print('\t'.join(t.name for t in te.keys()))

        for ranks in combos:
            self.clear_shapes()
            for t, r in zip(rng.keys(), ranks):
                t.set_rank(r)
            for tup in expr_tups:
                tup.calc_rank()
            # now, ranks are completely set
            for tup in te.keys():
                tup.gen_dims()
            valid = self.validate()
            shapes = '\t'.join(str(t.dims()) for t in te.keys())
            print(f'{shapes}\t{valid}')

    # validate the current rank + dims setting
    def validate(self):
        for st in self.statements:
            st.evaluate()
        tf_outputs = self.tf_call.value()
        z = zip(self.out_args, tf_outputs)
        valid = [ util.equal_tens(et.value(), tf_out, 1e-6) for et, tf_out in z ]
        return valid

    def set_ranks(self, rank_map):
        for tup, rank in rank_map.items():
            if tup not in self.tups:
                raise RuntimeError('Cannot set dims for unknown EinTup {tup}')
            if not self.tup(tup).primary():
                raise RuntimeError(
                    f'Cannot set rank for non-primary EinTup {tup}')
            self.tup(tup).set_rank(rank)

    def set_dims(self, dims_map):
        for name, dims in dims_map.items():
            self.tups[name].set_dims(dims)

    def set_one_dim(self, tup, ind, val):
        self.tup(tup).set_dim(ind, val)

    def maybe_add_tup(self, name, shadow_of=None):
        if name in self.tups:
            pass
        elif shadow_of is None:
            self.tups[name] = EinTup(name, self.min_dim, self.max_dim, None)
        elif shadow_of.name in self.tups:
            self.tups[name] = EinTup(name, self.min_dim, self.max_dim, shadow_of)
        else:
            raise RuntimeError(
                f'Runtime::maybe_add_tup - shadow_of \'{shadow_of}\' '
                f'provided but does not exist')
        return self.tups[name]

    def get_primary_tups(self):
        return [ tup for tup in self.tups.values() if tup.primary() ]

    def tup(self, eintup):
        if eintup not in self.tups:
            raise RuntimeError(
                    f'Runtime::tup() got unknown eintup name {eintup}')
        return self.tups[eintup]

    def dims(self, eintup):
        return self.tup(eintup).dims()

    def rank(self, eintup):
        return len(self.dims(eintup))

    def nelem(self, eintup):
        return self.tup(eintup).nelem()

if __name__ == '__main__':
    rt = Runtime()
    rt.set_ranks({'batch': 2, 'slice': 1})

