"""Zero disables a resource ceiling; it never disables a trading risk control."""
def reached(value, limit):
    return bool(limit and value >= limit)

def exceeds(value, limit):
    return bool(limit and value > limit)

def fraction(value, limit):
    return value / limit if limit else 0.
