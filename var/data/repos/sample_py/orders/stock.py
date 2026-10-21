class StockService:
    def exists(self, item_id):
        return self._load(item_id) is not None

    def decrease(self, item_id, qty):
        """재고를 qty만큼 차감한다."""
        row = self._load(item_id)
        if row is None or row["count"] < qty:
            raise RuntimeError("out of stock")
        self._save(item_id, row["count"] - qty)

    def increase(self, item_id, qty):
        row = self._load(item_id)
        self._save(item_id, row["count"] + qty)

    def _load(self, item_id):
        return {"count": 10}

    def _save(self, item_id, count):
        return None
