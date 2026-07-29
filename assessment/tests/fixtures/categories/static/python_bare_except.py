def parse_count(value: str) -> int | None:
    try:
        return int(value)
    except:  # noqa: E722 - intentional static-analysis fixture
        return None
