from klepa_core import app


def test_the_open_file_limit_is_raised_from_launchds_256(monkeypatch):
    limits = {"soft": 256, "hard": app.resource.RLIM_INFINITY}

    def getrlimit(which):
        return limits["soft"], limits["hard"]

    def setrlimit(which, values):
        limits["soft"] = values[0]

    monkeypatch.setattr(app.resource, "getrlimit", getrlimit)
    monkeypatch.setattr(app.resource, "setrlimit", setrlimit)
    assert app.raise_file_limit() == app.OPEN_FILES
    limits.update(soft=256, hard=1024)
    assert app.raise_file_limit() == 1024
    limits.update(soft=20000, hard=app.resource.RLIM_INFINITY)
    assert app.raise_file_limit() == 20000  # never lowered
