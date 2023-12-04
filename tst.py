import select
import time
from hhd.controller.physical.imu import AccelImu, GyroImu
from hhd.controller.virtual.ds5 import DualSense5Edge
from hhd.controller.physical.evdev import (
    LGO_TOUCHPAD_AXIS_MAP,
    LGO_TOUCHPAD_BUTTON_MAP,
    GenericGamepadEvdev,
)

p = DualSense5Edge()

a = AccelImu()
b = GyroImu()
c = GenericGamepadEvdev(0x17EF, 0x6182, "Generic X-Box pad")
d = GenericGamepadEvdev(
    0x17EF,
    0x6182,
    "  Legion Controller for Windows  Touchpad",
    btn_map=LGO_TOUCHPAD_BUTTON_MAP,
    axis_map=LGO_TOUCHPAD_AXIS_MAP,
    aspect_ratio=1,
)

REPORT_FREQ_MIN = 25
REPORT_FREQ_MAX = 450

REPORT_DELAY_MAX = 1 / REPORT_FREQ_MIN
REPORT_DELAY_MIN = 1 / REPORT_FREQ_MAX

fds = []
devs = []
fd_to_dev = {}


def prepare(m):
    fs = m.open()
    devs.append(m)
    fds.extend(fs)
    for f in fs:
        fd_to_dev[f] = m


try:
    prepare(a)
    prepare(b)
    prepare(c)
    prepare(d)
    prepare(p)

    while True:
        start = time.perf_counter()
        # Add timeout to call consumers a minimum amount of times per second
        r, _, _ = select.select(fds, [], [], REPORT_DELAY_MAX)
        evs = []
        to_run = set()
        for f in r:
            to_run.add(id(fd_to_dev[f]))

        for d in devs:
            if id(d) in to_run:
                evs.extend(d.produce(r))

        if evs:
            # TODO: Remove. For testing
            print(evs)
        p.consume(evs)

        # If unbounded, the total number of events per second is the sum of all
        # events generated by the producers.
        # For Legion go, that would be 100 + 100 + 500 + 30 = 730
        # Since the controllers of the legion go only update at 500hz, this is
        # wasteful.
        # By setting a target refresh rate for the report and sleeping at the
        # end, we ensure that even if multiple fds become ready close to each other
        # they are combined to the same report, limiting resource use.
        # Ideally, this rate is smaller than the report rate of the hardware controller
        # to ensure there is always a report from that ready during refresh
        elapsed = time.perf_counter() - start
        if elapsed < REPORT_DELAY_MIN:
            time.sleep(REPORT_DELAY_MIN - elapsed)
finally:
    for d in devs:
        d.close(True)
