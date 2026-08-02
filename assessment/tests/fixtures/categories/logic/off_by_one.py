def last_page(item_count: int, page_size: int) -> int:
    if item_count == 0:
        return 0
    return item_count // page_size + 1
