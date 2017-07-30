"""Test the Vector object."""
import math

import pytest
import operator as op
import srctools
from srctools import Vec_tuple


from typing import Type
Vec = ...  # type: Type[srctools.Vec]

VALID_NUMS = [
    1, 1.5, 0.2827, 2346.45,
]
VALID_NUMS += [-x for x in VALID_NUMS]

VALID_ZERONUMS = VALID_NUMS + [0, -0]

raises_typeerror = pytest.raises(TypeError)
raises_keyerror = pytest.raises(KeyError)
raises_zero_div = pytest.raises(ZeroDivisionError)


@pytest.fixture(params=[srctools.Vec])
def py_c_vec(request):  # type: ignore
    """Run the test twice, for the Python and C versions."""
    global Vec
    orig_vec = srctools.Vec
    Vec = request.param
    yield None
    Vec = orig_vec


def iter_vec(nums):
    for x in nums:
        for y in nums:
            for z in nums:
                yield x, y, z


def assert_vec(vec, x, y, z, msg=''):
    """Asserts that Vec is equal to (x,y,z)."""
    # Don't show in pytest tracebacks.
    __tracebackhide__ = True

    # Ignore slight variations
    if not vec.x == pytest.approx(x):
        failed = 'x'
    elif not vec.y == pytest.approx(y):
        failed = 'y'
    elif not vec.z == pytest.approx(z):
        failed = 'z'
    else:
        # Success!
        return

    new_msg = "{!r} != ({}, {}, {})".format(vec, failed, x, y, z)
    if msg:
        new_msg += ': ' + msg
    pytest.fail(new_msg)


def test_construction():
    """Check various parts of the constructor - Vec(), Vec.from_str()."""
    for x, y, z in iter_vec(VALID_ZERONUMS):
        assert_vec(Vec(x, y, z), x, y, z)
        assert_vec(Vec(x, y), x, y, 0)
        assert_vec(Vec(x), x, 0, 0)
        assert_vec(Vec(), 0, 0, 0)

        assert_vec(Vec([x, y, z]), x, y, z)
        assert_vec(Vec([x, y], z=z), x, y, z)
        assert_vec(Vec([x], y=y, z=z), x, y, z)
        assert_vec(Vec([x]), x, 0, 0)
        assert_vec(Vec([x, y]), x, y, 0)
        assert_vec(Vec([x, y, z]), x, y, z)
        # Check copying keeps the same values..
        assert_vec(Vec(x, y, z).copy(), x, y, z)

        # Test Vec.from_str()
        assert_vec(Vec.from_str('{} {} {}'.format(x, y, z)), x, y, z)
        assert_vec(Vec.from_str('<{} {} {}>'.format(x, y, z)), x, y, z)
        # {x y z}
        assert_vec(Vec.from_str('{{{} {} {}}}'.format(x, y, z)), x, y, z)
        assert_vec(Vec.from_str('({} {} {})'.format(x, y, z)), x, y, z)
        assert_vec(Vec.from_str('[{} {} {}]'.format(x, y, z)), x, y, z)

        # Test converting a converted Vec
        orig = Vec(x, y, z)
        new = Vec.from_str(Vec(x, y, z))
        assert_vec(new, x, y, z)
        assert orig is not new  # It must be a copy

        # Check as_tuple() makes an equivalent tuple
        tup = orig.as_tuple()
        assert isinstance(tup, tuple)
        assert (x, y, z) == tup
        assert hash((x, y, z)) == hash(tup)
        # Bypass subclass functions.
        assert tuple.__getitem__(tup, 0) == x
        assert tuple.__getitem__(tup, 1) == y
        assert tuple.__getitem__(tup, 2) == z
        assert tup.x == x
        assert tup.y == y
        assert tup.z == z

    # Check failures in Vec.from_str()
    # Note - does not pass through unchanged, they're converted to floats!
    for val in VALID_ZERONUMS:
        assert val == Vec.from_str('', x=val).x
        assert val == Vec.from_str('blah 4 2', y=val).y
        assert val == Vec.from_str('2 hi 2', x=val).x
        assert val == Vec.from_str('2 6 gh', z=val).z
        assert val == Vec.from_str('1.2 3.4', x=val).x
        assert val == Vec.from_str('34.5 38.4 -23 -38', z=val).z


def test_with_axes():
    """Test the with_axes() constructor."""
    for axis, u, v in ['xyz', 'yxz', 'zxy']:
        for num in VALID_ZERONUMS:
            vec = Vec.with_axes(axis, num)
            assert vec[axis] == num
            # Other axes are zero.
            assert vec[u] == 0
            assert vec[v] == 0

    for a, b, c in iter_vec('xyz'):
        if a == b or b == c or a == c:
            continue
        for x, y, z in iter_vec(VALID_ZERONUMS):
            vec = Vec.with_axes(a, x, b, y, c, z)
            assert vec[a] == x
            assert vec[b] == y
            assert vec[c] == z


def test_unary_ops():
    """Test -vec and +vec."""
    for x, y, z in iter_vec(VALID_NUMS):
        assert_vec(-Vec(x, y, z), -x, -y, -z)
        assert_vec(+Vec(x, y, z), +x, +y, +z)


def test_mag():
    """Test magnitude methods."""
    for x, y, z in iter_vec(VALID_NUMS):
        vec = Vec(x, y, z)
        mag = vec.mag()
        length = vec.len()
        assert mag == length
        assert mag == math.sqrt(x**2 + y**2 + z**2)

        mag_sq = vec.mag_sq()
        len_sq = vec.len_sq()
        assert mag_sq == len_sq
        assert len_sq == x**2 + y**2 + z**2


def test_contains():
    # Match to list.__contains__
    for num in VALID_NUMS:
        for x, y, z in iter_vec(VALID_NUMS):
            assert (num in Vec(x, y, z)) == (num in [x, y, z])


def test_scalar():
    """Check that Vec() + 5, -5, etc does the correct thing.

    For +, -, *, /, // and % calling with a scalar should perform the
    operation on x, y, and z
    """
    operators = [
        ('+', op.add, op.iadd, VALID_ZERONUMS),
        ('-', op.sub, op.isub, VALID_ZERONUMS),
        ('*', op.mul, op.imul, VALID_ZERONUMS),
        ('//', op.floordiv, op.ifloordiv, VALID_NUMS),
        ('/', op.truediv, op.itruediv, VALID_NUMS),
        ('%', op.mod, op.imod, VALID_NUMS),
    ]

    # Doesn't implement float(x), and no other operators..
    obj = object()

    for op_name, op_func, op_ifunc, domain in operators:
        for x, y, z in iter_vec(domain):
            for num in domain:
                targ = Vec(x, y, z)
                rx, ry, rz = (
                    op_func(x, num),
                    op_func(y, num),
                    op_func(z, num),
                )

                # Check forward and reverse fails.
                with pytest.raises(TypeError, message='forward ' + op_name):
                    op_func(targ, obj)
                with pytest.raises(TypeError, message='backward ' + op_name):
                    op_func(obj, targ)
                with pytest.raises(TypeError, message='inplace ' + op_name):
                    op_ifunc(targ, obj)

                assert_vec(
                    op_func(targ, num),
                    rx, ry, rz,
                    'Forward ' + op_name,
                )

                assert_vec(
                    op_func(num, targ),
                    op_func(num, x),
                    op_func(num, y),
                    op_func(num, z),
                    'Reversed ' + op_name,
                )

                # Ensure they haven't modified the original
                assert_vec(targ, x, y, z)

                assert_vec(
                    op_ifunc(targ, num),
                    rx, ry, rz,
                    'Return value for ({} {} {}) {}= {}'.format(
                        x, y, z, op_name, num,
                    ),
                )
                # Check that the original was modified..
                assert_vec(
                    targ,
                    rx, ry, rz,
                    'Original for ({} {} {}) {}= {}'.format(
                        x, y, z, op_name, num,
                    ),
                )


def test_vec_to_vec():
    """Check that Vec() +/- Vec() does the correct thing.

    For +, -, two Vectors apply the operations to all values.
    Dot and cross products do something different.
    """
    operators = [
        ('+', op.add, op.iadd),
        ('-', op.sub, op.isub),
    ]

    def test(x1, y1, z1, x2, y2, z2):
        """Check a Vec pair for the operations."""
        vec1 = Vec(x1, y1, z1)
        vec2 = Vec(x2, y2, z2)

        # These are direct methods, so no inheritence and iop to deal with.

        # Commutative
        assert vec1.dot(vec2) == (x1*x2 + y1*y2 + z1*z2)
        assert vec2.dot(vec1) == (x1*x2 + y1*y2 + z1*z2)
        assert_vec(
            vec1.cross(vec2),
            y1*z2-z1*y2,
            z1*x2-x1*z2,
            x1*y2-y1*x2,
        )
        # Ensure they haven't modified the originals
        assert_vec(vec1, x1, y1, z1)
        assert_vec(vec2, x2, y2, z2)

        # Addition and subtraction
        for op_name, op_func, op_ifunc in operators:
            result = (
                op_func(x1, x2),
                op_func(y1, y2),
                op_func(z1, z2),
            )
            assert_vec(
                op_func(vec1, vec2),
                *result,
                msg='Vec({} {} {}) {} Vec({} {} {})'.format(
                    x1, y1, z1, op_name, x2, y2, z2,
                )
            )
            # Ensure they haven't modified the originals
            assert_vec(vec1, x1, y1, z1)
            assert_vec(vec2, x2, y2, z2)

            assert_vec(
                op_func(vec1, Vec_tuple(x2, y2, z2)),
                *result,
                msg='Vec({} {} {}) {} Vec_tuple({} {} {})'.format(
                    x1, y1, z1, op_name, x2, y2, z2,
                )
            )
            assert_vec(vec1, x1, y1, z1)

            assert_vec(
                op_func(Vec_tuple(x1, y1, z1), vec2),
                *result,
                msg='Vec_tuple({} {} {}) {} Vec({} {} {})'.format(
                    x1, y1, z1, op_name, x2, y2, z2,
                )
            )

            assert_vec(vec2, x2, y2, z2)

            new_vec1 = Vec(x1, y1, z1)
            assert_vec(
                op_ifunc(new_vec1, vec2),
                *result,
                msg='Return val: ({} {} {}) {}= ({} {} {})'.format(
                    x1, y1, z1, op_name, x2, y2, z2,
                )
            )
            # Check it modifies the original object too.
            assert_vec(
                new_vec1,
                *result,
                msg='Original: ({} {} {}) {}= ({} {} {})'.format(
                    x1, y1, z1, op_name, x2, y2, z2,
                )
            )

            new_vec1 = Vec(x1, y1, z1)
            assert_vec(
                op_ifunc(new_vec1, tuple(vec2)),
                *result,
                msg='Return val: ({} {} {}) {}= tuple({} {} {})'.format(
                    x1, y1, z1, op_name, x2, y2, z2,
                )
            )
            # Check it modifies the original object too.
            assert_vec(
                new_vec1,
                *result,
                msg='Original: ({} {} {}) {}= tuple({} {} {})'.format(
                    x1, y1, z1, op_name, x2, y2, z2,
                )
            )

    for num in VALID_ZERONUMS:
        for num2 in VALID_ZERONUMS:
            # Test the whole value, then each axis individually
            test(num, num, num, num2, num2, num2)
            test(0, num, num, num2, num2, num2)
            test(num, 0, num, num, num2, num2)
            test(num, num, 0, num2, num2, num2)
            test(num, num, num, 0, num2, num2)
            test(num, num, num, num, 0, num2)
            test(num, num, num, num, num, 0)


def test_scalar_zero():
    """Check zero behaviour with division ops."""
    for x, y, z in iter_vec(VALID_NUMS):
        vec = Vec(x, y, z)
        assert_vec(0 / vec, 0, 0, 0)
        assert_vec(0 // vec, 0, 0, 0)
        assert_vec(0 % vec, 0, 0, 0)
        assert_vec(0.0 / vec, 0, 0, 0)
        assert_vec(0.0 // vec, 0, 0, 0)
        assert_vec(0.0 % vec, 0, 0, 0)

        # We don't need to check divmod(0, vec) -
        # that always falls back to % and /.

        with raises_zero_div: vec / 0
        with raises_zero_div: vec // 0
        with raises_zero_div: vec % 0
        with raises_zero_div: divmod(vec, 0)
        with raises_zero_div: vec / 0.0
        with raises_zero_div: vec // 0.0
        with raises_zero_div: vec % 0.0
        with raises_zero_div: divmod(vec, 0.0)

        with raises_zero_div: vec /= 0
        with raises_zero_div: vec //= 0
        with raises_zero_div: vec %= 0
        with raises_zero_div: vec /= 0.0
        with raises_zero_div: vec //= 0.0
        with raises_zero_div: vec %= 0.0


def test_order():
    """Test ordering operations (>, <, <=, >=, ==)."""
    comp_ops = [op.eq, op.le, op.lt, op.ge, op.gt, op.ne]

    def test(x1, y1, z1, x2, y2, z2):
        """Check a Vec pair for incorrect comparisons."""
        vec1 = Vec(x1, y1, z1)
        vec2 = Vec(x2, y2, z2)
        for op_func in comp_ops:
            if op_func is op.ne:
                # special-case - != uses or, not and
                corr_result = x1 != x2 or y1 != y2 or z1 != z2
            else:
                corr_result = op_func(x1, x2) and op_func(y1, y2) and op_func(z1, z2)
            comp = (
                'Incorrect {{}} comparison for '
                '({} {} {}) {} ({} {} {})'.format(
                    x1, y1, z1, op_func.__name__, x2, y2, z2
                )
            )
            assert op_func(vec1, vec2) == corr_result, comp.format('Vec')
            assert op_func(vec1, Vec_tuple(x2, y2, z2)) == corr_result, comp.format('Vec_tuple')
            assert op_func(vec1, (x2, y2, z2)) == corr_result, comp.format('tuple')
            # Bare numbers compare magnitude..
            assert op_func(vec1, x2) == op_func(vec1.mag(), x2), comp.format('x')
            assert op_func(vec1, y2) == op_func(vec1.mag(), y2), comp.format('y')
            assert op_func(vec1, z2) == op_func(vec1.mag(), z2), comp.format('z')

    for num in VALID_ZERONUMS:
        for num2 in VALID_ZERONUMS:
            # Test the whole comparison, then each axis pair seperately
            test(num, num, num, num2, num2, num2)
            test(0, num, num, num2, num2, num2)
            test(num, 0, num, num, num2, num2)
            test(num, num, 0, num2, num2, num2)
            test(num, num, num, 0, num2, num2)
            test(num, num, num, num, 0, num2)
            test(num, num, num, num, num, 0)


def test_binop_fail():
    """Test binary operations with invalid operands."""
    vec = Vec()
    operations = [
        op.add, op.iadd,
        op.sub, op.isub,
        op.truediv, op.itruediv,
        op.floordiv, op.ifloordiv,
        op.mul, op.imul,
        op.lt, op.gt,
        op.le, op.ge,

        divmod,
        op.concat, op.iconcat,
    ]
    for fail_object in [None, 'string', ..., staticmethod, tuple, Vec]:
        assert vec != fail_object
        assert fail_object != vec

        assert not vec == fail_object
        assert not fail_object == vec
        for operation in operations:
            pytest.raises(TypeError, operation, vec, fail_object)
            pytest.raises(TypeError, operation, fail_object, vec)


def test_axis():
    """Test the Vec.axis() function."""
    assert Vec(1, 0, 0).axis() == 'x'
    assert Vec(-1, 0, 0).axis() == 'x'
    assert Vec(0, 1, 0).axis() == 'y'
    assert Vec(0, -1, 0).axis() == 'y'
    assert Vec(0, 0, 1).axis() == 'z'
    assert Vec(0, 0, -1).axis() == 'z'


def test_other_axes():
    """Test Vec.other_axes()."""
    bad_args = ['p', '', 0, 1, 2, False, Vec(2, 3, 5)]
    for x, y, z in iter_vec(VALID_NUMS):
        vec = Vec(x, y, z)
        assert vec.other_axes('x') == (y, z)
        assert vec.other_axes('y') == (x, z)
        assert vec.other_axes('z') == (x, y)
        # Test some bad args.
        for invalid in bad_args:
            with raises_keyerror: vec.other_axes(invalid)


def test_abs():
    """Test the function of abs(Vec)."""
    for x, y, z in iter_vec(VALID_ZERONUMS):
        assert_vec(abs(Vec(x, y, z)), abs(x), abs(y), abs(z))


def test_bool():
    """Test bool() applied to Vec."""
    # Empty vector is False
    assert not Vec(0, 0, 0)
    assert not Vec(-0, -0, -0)
    for val in VALID_NUMS:
        # Any number in any axis makes it True.
        assert Vec(val, -0, 0)
        assert Vec(0, val, 0)
        assert Vec(-0, 0, val)
        assert Vec(0, val, val)
        assert Vec(val, -0, val)
        assert Vec(val, val, 0)
        assert Vec(val, val, val)


def test_len():
    """Test len(Vec)."""
    # len(Vec) is the number of non-zero axes.

    assert len(Vec(0, 0, 0)) == 0
    assert len(Vec(-0, -0, -0)) == 0

    for val in VALID_NUMS:
        assert len(Vec(val, 0, -0)) == 1
        assert len(Vec(0, val, 0)) == 1
        assert len(Vec(0, -0, val)) == 1
        assert len(Vec(0, val, val)) == 2
        assert len(Vec(val, 0, val)) == 2
        assert len(Vec(val, val, -0)) == 2
        assert len(Vec(val, val, val)) == 3


def test_getitem():
    """Test vec[x] with various args."""
    a = 1.8
    b = 2.3
    c = 3.6
    vec = Vec(a, b, c)

    assert vec[0] == a
    assert vec[1] == b
    assert vec[2] == c

    assert vec['x'] == a
    assert vec['y'] == b
    assert vec['z'] == c

    for invalid in ['4', '', -1, 4, 4.0, bool, slice(0, 1), Vec(2,3,4)]:
        with raises_keyerror: vec[invalid]


def test_setitem():
    """Test vec[x]=y with various args."""
    for ind, axis in enumerate('xyz'):
        vec1 = Vec()
        vec1[axis] = 20.3
        assert vec1[axis] == 20.3
        assert vec1.other_axes(axis) == (0.0, 0.0)

        vec2 = Vec()
        vec2[ind] = 20.3
        assert vec1 == vec2

    vec = Vec()
    for invalid in ['4', '', -1, 4, 4.0, bool, slice(0, 1), Vec(2,3,4)]:
        with raises_keyerror: vec[invalid] = 8


def test_vec_constants():
    """Check some of the constants assigned to Vec."""
    assert Vec.N == Vec.north == Vec(y=1)
    assert Vec.S == Vec.south == Vec(y=-1)
    assert Vec.E == Vec.east == Vec(x=1)
    assert Vec.W == Vec.west == Vec(x=-1)

    assert Vec.T == Vec.top == Vec(z=1)
    assert Vec.B == Vec.bottom == Vec(z=-1)

    assert set(Vec.INV_AXIS['x']) == {'y', 'z'}
    assert set(Vec.INV_AXIS['y']) == {'x', 'z'}
    assert set(Vec.INV_AXIS['z']) == {'x', 'y'}

    assert Vec.INV_AXIS['x', 'y'] == 'z'
    assert Vec.INV_AXIS['y', 'z'] == 'x'
    assert Vec.INV_AXIS['x', 'z'] == 'y'

    assert Vec.INV_AXIS['y', 'x'] == 'z'
    assert Vec.INV_AXIS['z', 'y'] == 'x'
    assert Vec.INV_AXIS['z', 'x'] == 'y'

# Copied from CPython's round() tests.
ROUND_VALS = [
    (1.0, 1.0),
    (10.0, 10.0),
    (1000000000.0, 1000000000.0),
    (1e20, 1e20),

    (-1.0, -1.0),
    (-10.0, -10.0),
    (-1000000000.0, -1000000000.0),
    (-1e20, -1e20),

    (0.1, 0.0),
    (1.1, 1.0),
    (10.1, 10.0),
    (1000000000.1, 1000000000.0),

    (-1.1, -1.0),
    (-10.1, -10.0),
    (-1000000000.1, -1000000000.0),

    (0.9, 1.0),
    (9.9, 10.0),
    (999999999.9, 1000000000.0),

    (-0.9, -1.0),
    (-9.9, -10.0),
    (-999999999.9, -1000000000.0),

    # Even/odd rounding behaviour..
    (5.5, 6),
    (6.5, 6),
    (-5.5, -6),
    (-6.5, -6),

    (5e15 - 1, 5e15 - 1),
    (5e15, 5e15),
    (5e15 + 1, 5e15 + 1),
    (5e15 + 2, 5e15 + 2),
    (5e15 + 3, 5e15 + 3),
]


def test_round():
    """Test round(Vec)."""
    for from_val, to_val in ROUND_VALS:
        assert round(Vec(from_val, from_val, from_val)) == Vec(to_val, to_val, to_val)

    # Check it doesn't mix up orders..
    for val in VALID_NUMS:
        assert round(Vec(val, 0, 0)) == Vec(round(val), 0, 0)
        assert round(Vec(0, val, 0)) == Vec(0, round(val), 0)
        assert round(Vec(0, 0, val)) == Vec(0, 0, round(val))

MINMAX_VALUES = [
    (0, 0),
    (1, 0),
    (-5, -5),
    (0.3, 0.4),
    (-0.3, -0.2),
]
MINMAX_VALUES += [(b, a) for a,b in MINMAX_VALUES]


def test_minmax():
    """Test Vec.min() and Vec.max()."""
    vec_a = Vec()
    vec_b = Vec()

    for a, b in MINMAX_VALUES:
        max_val = max(a, b)
        min_val = min(a, b)
        for axis in 'xyz':
            vec_a.x = vec_a.y = vec_a.z = 0
            vec_b.x = vec_b.y = vec_b.z = 0

            vec_a[axis] = a
            vec_b[axis] = b
            assert vec_a.min(vec_b) is None, (a, b, axis, min_val)
            assert vec_a[axis] == min_val, (a, b, axis, min_val)

            vec_a[axis] = a
            vec_b[axis] = b
            assert vec_a.max(vec_b) is None, (a, b, axis, max_val)
            assert vec_a[axis] == max_val, (a, b, axis, max_val)
