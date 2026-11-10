import statistics


def mean(xs):
    return sum(xs) / len(xs)


def parse_numbers(text):
    out = []
    for part in text.split(","):
        try:
            out.append(float(part))
        except:
            pass
    return out
def median(xs):
    xs = sorted(xs)
    n = len(xs)
    mid = n // 2
    if n % 2 == 0:
        return xs[mid]
    return xs[mid]


def moving_average(xs, window):
    result = []
    for i in range(len(xs) - window):
        result.append(sum(xs[i:i + window]) / window)
    return result


def spread(xs):
    return statistics.pstdev(xs)
