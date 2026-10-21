import os


def _api_key():
    return os.environ.get("PAYMENT_API_KEY", "")


class PaymentClient:
    def request(self, user_id, item_id, qty):
        """결제 게이트웨이에 결제를 요청한다."""
        key = _api_key()
        return self._post("/pay", {"user": user_id, "item": item_id, "qty": qty, "key": key})

    def refund(self, user_id, item_id, qty):
        return self._post("/refund", {"user": user_id, "item": item_id, "qty": qty})

    def _post(self, path, body):
        return True
