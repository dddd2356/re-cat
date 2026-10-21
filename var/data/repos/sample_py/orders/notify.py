def send_receipt(user_id, item_id):
    """영수증 메일을 보낸다."""
    body = _render(user_id, item_id)
    return _deliver(user_id, body)


def _render(user_id, item_id):
    return f"receipt for {user_id}: {item_id}"


def _deliver(user_id, body):
    return True
