from homelab.jobs.schema import ensure_schema


class FakeSchemaManager:
    def __init__(self):
        self.applied = False

    def apply_schema(self):
        self.applied = True


class FakeApp:
    def __init__(self, schema_exists):
        self.schema_exists = schema_exists
        self.schema_manager = FakeSchemaManager()

    def check_connection(self):
        return self.schema_exists


def test_ensure_schema_applies_when_absent():
    app = FakeApp(schema_exists=False)

    assert ensure_schema(app) is True
    assert app.schema_manager.applied is True


def test_ensure_schema_skips_when_present():
    app = FakeApp(schema_exists=True)

    assert ensure_schema(app) is False
    assert app.schema_manager.applied is False
