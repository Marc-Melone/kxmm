from kxmm.keepawake import keep_awake


class FakeKernel32:
    def __init__(self):
        self.calls = []

    def SetThreadExecutionState(self, flags):  # noqa: N802 (Win32 name)
        self.calls.append(flags)
        return 1


class FakeProc:
    def __init__(self, args):
        self.args = args
        self.terminated = False

    def terminate(self):
        self.terminated = True


class FakePopen:
    def __init__(self):
        self.procs = []

    def __call__(self, args, **kwargs):
        p = FakeProc(args)
        self.procs.append(p)
        return p


def test_keep_awake_windows_sets_and_clears():
    k = FakeKernel32()
    with keep_awake(platform="win32", kernel32=k):
        assert k.calls == [0x80000001]
    assert k.calls == [0x80000001, 0x80000000]


def test_keep_awake_mac_starts_and_stops_caffeinate():
    popen = FakePopen()
    with keep_awake(platform="darwin", popen=popen):
        assert len(popen.procs) == 1
        assert popen.procs[0].args[:3] == ["caffeinate", "-i", "-w"]
        assert not popen.procs[0].terminated
    assert popen.procs[0].terminated


def test_keep_awake_other_is_noop():
    k, popen = FakeKernel32(), FakePopen()
    with keep_awake(platform="linux", kernel32=k, popen=popen):
        pass
    assert k.calls == [] and popen.procs == []
