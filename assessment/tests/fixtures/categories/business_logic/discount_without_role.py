def apply_discount(user, total, discount):
    if discount <= 0:
        return total
    return total - discount
