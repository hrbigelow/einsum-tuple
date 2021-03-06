import tensorflow as tf
import numpy as np
from itertools import accumulate
import operator
import math

# Assume inds[...,i] = c[i], compute flat[...] = WRAP(c, digits)
def _flatten(inds, dest_dims):
    if inds.shape[-1] != len(dest_dims):
        raise RuntimeError(
            f'flatten: last dimension size must equal number of dest_dims. '
            f'Got inds.shape[-1] = {inds.shape[-1]} and '
            f'digit sizes {dest_dims}')
    accu = accumulate(dest_dims, operator.mul)
    prod = np.prod(dest_dims, dtype=np.int32)
    mult = tf.constant([prod // r for r in accu], dtype=tf_int)
    inds = tf.multiply(inds, mult)
    inds = tf.reduce_sum(inds, -1, keepdims=True)
    return inds

# return True if 0 <= inds[...,i] < dest_dims[i] 
def _range_check(inds, dest_dims):
    lim = tf.constant(dest_dims, dtype=tf_int)
    below = tf.less(inds, lim)
    above = tf.greater_equal(inds, 0)
    below = tf.reduce_all(below, axis=-1, keepdims=True)
    above = tf.reduce_all(above, axis=-1, keepdims=True)
    in_bounds = tf.logical_and(above, below)
    return in_bounds

# flatten an index tensor's inner dimension, assuming it is
# used in the destination signature
def flatten_with_bounds(index_ten, dest_sig):
    dest_dims = single_dims(dest_sig)
    in_bounds = _range_check(index_ten, dest_dims)
    index_ten = _flatten(index_ten, dest_dims)
    index_ten = tf.where(in_bounds, index_ten, -1)
    return index_ten 

def union_ixn(a, b):
    a_extra = [ el for el in a if el not in b ]
    b_extra = [ el for el in b if el not in a ]
    ab_ixn =  [ el for el in a if el in b ]
    return a_extra, ab_ixn, b_extra

def broadcastable(array_dims, sig_dims):
    if len(array_dims) != len(sig_dims):
        return False
    return all(ad in (1, sd) for ad, sd in zip(array_dims, sig_dims))

def ndrange(dims):
    ten = [tf.range(e, dtype=tf_int) for e in dims]
    ten = tf.meshgrid(*ten, indexing='ij')
    ten = tf.stack(ten, axis=len(dims))
    ten = tf.cast(ten, dtype=tf_int) # needed when tf_stack gets empty list
    return ten

def single_dims(shapes):
    # shape.dims() may be empty, but this still works correctly
    return [ dim for shape in shapes for dim in shape.dims()]

def packed_dims(shapes):
    # shape.nelem() returns 1 for a zero-rank shape.  this
    # seems to work correctly.
    return [ shape.nelem() for shape in shapes ]

# Expect a list of lists of ShapeExpr
def packed_dims_nested(shapes_nested):
    return [ np.prod(packed_dims(sl), dtype=np.int32) for sl in shapes_nested ]

def pack(ten, sig):
    check_shape(ten, sig, is_packed=False)
    return tf.reshape(ten, packed_dims(sig))

# nested_sig is a list of lists of ShapeExprs.
# the individual sigs must appear in the same order as
# the original shape
def pack_nested(ten, nested_sig):
    flat_sig = [ sig for sl in nested_sig for sig in sl ]
    check_shape(ten, flat_sig, is_packed=False)
    dims = packed_dims_nested(nested_sig)
    return tf.reshape(ten, dims)

def safe_pad(ten, pads, constant_values):
    if len(pads) != ten.shape.rank:
        raise RuntimeError(
            f'pad: must be the same number of pads as tensor rank.'
            f'got {len(pads)} and {ten.shape.rank}')

    rank = len(pads)
    flat = ten.shape.as_list()
    # we use 6 because the max rank for tf.pad is 8
    for lo in range(0, rank, 6):
        hi = lo + 6
        new_pad = [[0,0]] + pads[lo:hi] + [[0,0]]
        shape = ([np.prod(flat[:lo], dtype=np.int32)] + flat[lo:hi] +
                [np.prod(flat[hi:], dtype=np.int32)])
        ten = tf.reshape(ten, shape)
        ten = tf.pad(ten, new_pad, constant_values=constant_values)

    new_shape = [ f + p[0] + p[1] for f, p in zip(flat, pads) ]
    ten = tf.reshape(ten, new_shape)
    return ten

# overwrite (or accumulate) the values in trg_ten with those in src_ten if the
# coordinates match.  ignore any positions in src_ten that are out-of-bounds in 
# trg_ten
def fit_to_size(src_ten, trg_ten, do_add):
    if src_ten.shape.rank != trg_ten.shape.rank:
        raise RuntimeError(
            f'ranks must match between source and target tensors.'
            f'got {src_ten.shape.rank} and {trg_ten.shape.rank}')

    src_dims = src_ten.shape.as_list()
    trg_dims = trg_ten.shape.as_list()
    begin = tf.zeros([len(src_dims)], dtype=tf.int32)
    trim_dims = [ min(src, trg) for src, trg in zip(src_dims, trg_dims) ]
    src_ten = tf.slice(src_ten, begin, trim_dims)
    paddings = [ [0, trg - trim] for trim, trg in zip(trim_dims, trg_dims) ]
    # pad_ten = tf.constant(paddings, shape=[len(paddings), 2], dtype=tf.int32)
    src_ten = safe_pad(src_ten, paddings, 0)
    # src_ten = tf.pad(src_ten, pad_ten, constant_values=0) 

    if do_add:
        return tf.add(src_ten, trg_ten)
    else:
        mask = tf.constant([True], shape=trim_dims)
        mask = safe_pad(mask, paddings, constant_values=False)
        return tf.where(mask, src_ten, trg_ten)

# used to construct a slightly order-preserving signature for
# the result of a binary op
def merge_tup_lists(a, b):
    ait, bit = iter(a), iter(b)
    ae = next(ait, None)
    be = next(bit, None)
    out = []
    while ae is not None or be is not None:
        if ae is None:
            if be not in out:
                out.append(be)
            be = next(bit, None)
        else:
            out.append(ae)
            ae = next(ait, None)
    return out


# check tensor shape against sig
def check_shape(ten, sig, is_packed):
    expect_dims = packed_dims(sig) if is_packed else single_dims(sig)
    if ten.shape.as_list() != expect_dims:
        desc = 'packed' if is_packed else 'flat'
        raise RuntimeError(
            f'Tensor shape {ten.shape.as_list()} not consistent with '
            f'signature {desc} shape {expect_dims}')
    

# reshape / transpose ten, with starting shape src_sig, to shape trg_sig.  if
# in_packed, expect ten shape to be in the packed form of src_sig.  produce a
# tensor with either packed or flat form of trg_sig
def to_sig(ten, src_sig, trg_sig, in_packed=False, out_packed=False):
    check_shape(ten, src_sig, in_packed)

    if not in_packed:
        src_dims = packed_dims(src_sig)
        ten = tf.reshape(ten, src_dims)

    marg_ex = set(src_sig).difference(trg_sig)
    if len(marg_ex) != 0:
        marg_pos = [ i for i, tup in enumerate(src_sig) if tup in marg_ex ]
        ten = tf.reduce_sum(ten, marg_pos)

    src_sig = [ tup for tup in src_sig if tup not in marg_ex ]
    card = packed_dims(src_sig)
    augmented = list(src_sig)
    trg_dims = []

    for ti, trg in enumerate(trg_sig):
        if trg not in src_sig:
            card.append(1)
            augmented.append(trg)
            trg_dims.extend([1] * trg.rank())
        else:
            trg_dims.extend(trg.dims())

    # trg_sig[i] = augmented[perm[i]], maps augmented to trg_sig
    perm = []
    for trg in trg_sig:
        perm.append(augmented.index(trg))

    ten = tf.reshape(ten, card)
    ten = tf.transpose(ten, perm)

    if not out_packed:
        ten = tf.reshape(ten, trg_dims)
        ten = tf.broadcast_to(ten, single_dims(trg_sig))
    else:
        ten = tf.broadcast_to(ten, packed_dims(trg_sig))

    return ten

def equal_tens(a, b, eps):
    if not a.dtype.is_floating:
        eps = 0
    if a.shape != b.shape:
        print(f'equal_tens: {a.shape} != {b.shape}')
    return (
            a.shape == b.shape and
            tf.reduce_all(tf.less_equal(tf.abs(a - b), eps)).numpy()
            )

def maybe_broadcast(a, length):
    if isinstance(a, (list, tuple)):
        if len(a) != length:
            raise RuntimeError(
                f'Cannot broadcast {a} to length {length}')
        else:
            return a
    else:
        return [a] * length

def ceildiv(a, b):
    return math.ceil(a / b)

def ceildiv_tensor(a, b):
    return tf.math.ceil(a / b)

ops = { 
        '+': tf.add,
        '-': tf.subtract,
        '*': tf.multiply,
        '//': tf.math.floordiv,
        '//^': ceildiv_tensor,
        '%': tf.math.floormod
        }

scalar_ops = {
        '+': operator.add,
        '-': operator.sub,
        '*': operator.mul,
        '//': operator.floordiv,
        '//^': ceildiv,
        '%': operator.mod
        }

# tf_int = tf.int64
tf_int = tf.int32

