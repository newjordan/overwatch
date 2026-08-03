import time
import unittest

import overwatch as ow


def probe_text(hydra_json=""):
    return (
        "==GPUKIND==\nnvidia\n==GPU==\nRTX 3090, 42, 1900, 24576, 58, 210\n"
        "==CPU==\ncpu  100 0 50 800 20 0 0 0 0 0\ncpu0 50 0 25 400 10 0 0 0 0 0\n"
        "==NPROC==\n32\n==LOAD==\n0.5 0.4 0.3 1/900 1234\n"
        "==MEM==\nMemTotal: 32000000 kB\nMemAvailable: 28000000 kB\n"
        "==UPTIME==\n5000.5\n==HYDRA==\n" + hydra_json + "\n==END==\n"
    )


HYDRA_STATUS = (
    '{"ok": true, "identity": {"node_id": "abcd1234-rest", "hostname": "rig"},'
    ' "counts": {"artifacts": 3, "model_surfaces": 7, "peers": 5,'
    ' "model_health": 100, "dead_letters": 1, "inbox": 3, "outbox": 2},'
    ' "inbox_by_status": {"new": 2, "acked": 1},'
    ' "outbox_by_status": {"queued": 2}}'
)


def fake_node(name, **kw):
    base = ow._blank({"name": name, "source": "fleet", "is_self": False,
                      "online": True})
    base.update({"status": "ok", "cpu_pct": 20.0, "ncpu": 8,
                 "core_pct": [10.0, 30.0, None, 50.0],
                 "gpus": [{"name": "RTX", "util": 40.0, "mem_used": 2048.0,
                           "mem_total": 8192.0, "temp": 50.0, "power": 100.0}]})
    base.update(kw)
    return base


class HydraParseTests(unittest.TestCase):
    def test_probe_with_head(self):
        m = ow._parse_metrics(ow._parse_probe(probe_text(HYDRA_STATUS)))
        h = m["hydra"]
        self.assertEqual(h["inbox_new"], 2)
        self.assertEqual(h["inbox_acked"], 1)
        self.assertEqual(h["outbox_queued"], 2)
        self.assertEqual(h["surfaces"], 7)
        self.assertEqual(h["dead"], 1)
        self.assertEqual(h["node_id"], "abcd1234")

    def test_probe_without_head(self):
        m = ow._parse_metrics(ow._parse_probe(probe_text("")))
        self.assertIsNone(m["hydra"])
        self.assertEqual(len(m["gpus"]), 1)          # hydra section never breaks metrics

    def test_probe_with_garbage(self):
        m = ow._parse_metrics(ow._parse_probe(probe_text("not json at all")))
        self.assertIsNone(m["hydra"])

    def test_hydra_row_and_summary(self):
        h = {"ok": True, "inbox_new": 2, "inbox_acked": 1, "outbox_queued": 0,
             "artifacts": 3, "surfaces": 7, "peers": 5, "health": 9, "dead": 0}
        row = ow.hydra_row(h)
        text = "".join(t for t, _k in row)
        self.assertIn("✉2", text)
        self.assertIn("◈7", text)
        self.assertIsNone(ow.hydra_row(None))
        snap = {"a": fake_node("a", hydra=h), "b": fake_node("b")}
        self.assertEqual(ow.hydra_summary(snap), (1, 2, 0))

    def test_iso_age(self):
        self.assertEqual(ow._iso_age(None), "—")
        self.assertEqual(ow._iso_age("garbage"), "—")
        self.assertNotEqual(ow._iso_age("2026-01-01T00:00:00Z"), "—")


class FleetSignalTests(unittest.TestCase):
    def setUp(self):
        ow.FLEET_HIST.clear()
        ow._BEAT_EVENTS.clear()
        ow._SIG_ENV.update({"t": None, "bass": 0.0, "mid": 0.0, "treble": 0.0, "amp": 0.0})

    def test_bins_and_signal_shape(self):
        snap = {"a": fake_node("a"), "b": fake_node("b", status="offline")}
        bins = ow._signal_bins(snap)
        self.assertEqual(len(bins), 4)               # 1 GPU + 3 non-None cores, live node only
        self.assertTrue(bins[0][2])                  # GPU bin first
        now = time.time()
        ow.note_fleet_sample(snap, now)
        sig = ow.fleet_signal(snap, now)
        for k in ("bass", "mid", "treble", "amplitude", "beat_env"):
            self.assertGreaterEqual(sig[k], 0.0)
            self.assertLessEqual(sig[k], 1.0)
        self.assertEqual(len(sig["wave_gpu"]), 160)

    def test_gpu_jump_registers_beat(self):
        snap = {"a": fake_node("a")}
        ow.note_fleet_sample(snap, time.time() - 1)
        snap["a"]["gpus"][0]["util"] = 90.0
        ow.note_fleet_sample(snap, time.time())
        self.assertTrue(ow.beat_envelope(time.time()) > 0.5)


class ChannelTests(unittest.TestCase):
    def setUp(self):
        ow.FLEET_HIST.clear()
        self.snap = {"a": fake_node("a", is_self=True),
                     "b": fake_node("b"),
                     "c": fake_node("c", status="offline")}
        ow.note_fleet_sample(self.snap, time.time())
        self.sig = ow.fleet_signal(self.snap, time.time())

    def test_every_channel_every_size(self):
        for w, h in ((190, 48), (100, 30), (48, 12), (20, 5), (5, 2)):
            for name, fn, _d in ow.CHANNELS:
                rows = fn(w, h, self.sig, self.snap, time.time())
                self.assertEqual(len(rows), h, name)
                for r in rows:
                    self.assertEqual(len(r), w, name)

    def test_channel_selection(self):
        self.assertTrue(ow.set_channel("mandala"))
        self.assertEqual(ow.CHANNELS[ow.CHANNEL_IDX][0], "mandala")
        self.assertFalse(ow.set_channel("nope"))

    def test_blit_skips_blanks_and_coalesces(self):
        rows = [[("⠀", ow.FRAME), ("x", ow.TEXT), ("y", ow.TEXT), (" ", ow.DIM), ("z", ow.OK)]]
        calls = []
        ow.blit_rows(lambda y, x, s, k: calls.append((y, x, s, k)), rows, 3, 2, 10)
        self.assertEqual(calls, [(3, 3, "xy", ow.TEXT), (3, 6, "z", ow.OK)])


if __name__ == "__main__":
    unittest.main()
