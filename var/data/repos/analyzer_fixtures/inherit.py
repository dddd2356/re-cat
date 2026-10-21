class Base:
    def greet(self):
        return "hi"


class Child(Base):
    def call_greet(self):
        return self.greet()

    def call_missing(self):
        return self.totally_absent()
