"""Statistics the standard library does not provide.

Only Student's t critical values, which are needed for a confidence interval
on a fitted slope. A table rather than a dependency: scipy is a large addition
for one function, and thirty numbers anyone can check against a textbook are
more auditable than an opaque call.
"""

# Two-sided 95% critical values by degrees of freedom.
_T_95: dict[int, float] = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}
# Above 30 the value falls slowly towards the normal 1.96.
_T_95_LARGE: list[tuple[int, float]] = [(40, 2.021), (60, 2.000), (120, 1.980)]
_T_95_INFINITY = 1.960

CONFIDENCE_LEVEL = 0.95


def t_critical_95(degrees_of_freedom: int) -> float:
    """Two-sided 95% critical value.

    Deliberately not a normal approximation. With the engine's minimum of 3
    samples the degrees of freedom is 1, where t is 12.706 against the normal's
    1.96 -- using the normal there would understate the interval sevenfold and
    make three noisy laps look like a confident measurement.
    """
    if degrees_of_freedom < 1:
        raise ValueError("t needs at least one degree of freedom")
    if degrees_of_freedom in _T_95:
        return _T_95[degrees_of_freedom]
    for threshold, value in _T_95_LARGE:
        if degrees_of_freedom <= threshold:
            return value
    return _T_95_INFINITY
