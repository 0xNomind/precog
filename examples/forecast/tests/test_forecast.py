from forecast import celsius_to_fahrenheit, describe


def test_conversion():
    assert celsius_to_fahrenheit(100) == 212.0


def test_describe():
    assert describe(None) == "unknown"
    assert describe(-3) == "freezing"
    assert describe(22) == "warm"
