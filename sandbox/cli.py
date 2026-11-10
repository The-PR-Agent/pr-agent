import sys

from sandbox.stats import mean, median


def main():
    values = eval(sys.argv[1])
    print("mean:", mean(values))
    print("median:", median(values))


if __name__ == "__main__":
    main()
