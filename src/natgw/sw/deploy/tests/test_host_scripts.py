# SPDX-License-Identifier: BSD-3-Clause
"""natgw-fpga-hook.sh and natgw-fpga-update against a fake host: a fake
sysfs for the card and stand-ins for pyrite, lspci, qm, logger and modprobe
that record their arguments."""

import os
import stat
import subprocess

import pytest

HOST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "host")
HOOK = os.path.join(HOST, "natgw-fpga-hook.sh")
UPDATE = os.path.join(HOST, "natgw-fpga-update")
BDF = "0000:41:00.0"

FAKE = {
    # pyrite: record; fail the write (-w) or boot (-b) on request
    "pyrite": '''#!/bin/sh
echo "pyrite $*" >> "$CALLS"
case " $* " in
  *" -w "*) [ "${FAIL_WRITE:-0}" = 1 ] && exit 1 ;;
  *" -b "*) [ "${FAIL_BOOT:-0}" = 1 ] && exit 1 ;;
esac
exit 0
''',
    "lspci": '''#!/bin/sh
echo "lspci $*" >> "$CALLS"
echo "41:00.0 0200: ${CARD_ID_SEEN:-1234:c001}"
''',
    "qm": '''#!/bin/sh
echo "qm $*" >> "$CALLS"
''',
    "logger": "#!/bin/sh\nexit 0\n",
    "modprobe": "#!/bin/sh\nexit 0\n",
    "journalctl": "#!/bin/sh\nexit 0\n",
}


@pytest.fixture
def host(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in FAKE.items():
        p = bin_dir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    sysfs = tmp_path / "sys"
    dev = sysfs / "bus/pci/devices" / BDF
    dev.mkdir(parents=True)
    (dev / "config").write_text("")
    (dev / "driver_override").write_text("")
    for d in ("vfio-pci", "cndm"):
        (sysfs / "bus/pci/drivers" / d).mkdir(parents=True)
        (sysfs / "bus/pci/drivers" / d / "unbind").write_text("")
    (sysfs / "bus/pci/drivers_probe").write_text("")
    os.symlink(sysfs / "bus/pci/drivers/vfio-pci", dev / "driver")
    state = tmp_path / "state"
    (state / "pending").mkdir(parents=True)
    conf = tmp_path / "fpga.conf"

    class H:
        pass
    h = H()
    h.tmp, h.sysfs, h.dev, h.state, h.conf = tmp_path, sysfs, dev, state, conf
    h.calls = tmp_path / "calls"
    h.calls.write_text("")

    def configure(**kw):
        vals = {"NATGW_VMID": "100", "NATGW_CARD_BDF": BDF, "NATGW_RELOAD_ON_START": "no", "NATGW_STRICT": "no"}
        vals.update(kw)
        conf.write_text("".join(f"{k}={v}\n" for k, v in vals.items()))
    h.configure = configure
    configure()

    def run(script, *args, **env):
        e = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", CALLS=str(h.calls),
                 NATGW_SYSFS=str(sysfs), NATGW_FPGA_STATE=str(state), NATGW_FPGA_CONF=str(conf))
        e.update({k: str(v) for k, v in env.items()})
        return subprocess.run([script, *args], capture_output=True, text=True, env=e)
    h.run = run

    def pyrite_calls():
        return [c for c in h.calls.read_text().splitlines() if c.startswith("pyrite")]
    h.pyrite_calls = pyrite_calls
    return h


def stage(h, name="fpga.bit", data=b"bitstream"):
    p = h.state / "pending" / name
    p.write_bytes(data)
    return p


def test_hook_ignores_other_vms_and_phases(host):
    stage(host)
    assert host.run(HOOK, "101", "pre-start").returncode == 0
    for phase in ("post-start", "pre-stop", "post-stop"):
        assert host.run(HOOK, "100", phase).returncode == 0
    assert host.pyrite_calls() == []


def test_hook_without_update_only_checks_the_card(host):
    r = host.run(HOOK, "100", "pre-start")
    assert r.returncode == 0, r.stderr
    assert host.pyrite_calls() == []
    assert "ready (vfio-pci)" in r.stdout


def test_hook_flashes_then_reloads_and_keeps_the_image(host):
    stage(host, data=b"new image")
    r = host.run(HOOK, "100", "pre-start")
    assert r.returncode == 0, r.stdout + r.stderr
    assert host.pyrite_calls() == [f"pyrite -s {BDF} -w {host.state}/pending/fpga.bit -y",
                                   f"pyrite -s {BDF} -b -y"]
    assert list((host.state / "pending").iterdir()) == []
    assert (host.state / "current.bit").read_bytes() == b"new image"
    (hist,) = (host.state / "history").iterdir()
    assert hist.name.endswith("-fpga.bit") and hist.read_bytes() == b"new image"


def test_hook_failed_write_keeps_the_old_image_and_does_not_reload(host):
    stage(host)
    r = host.run(HOOK, "100", "pre-start", FAIL_WRITE=1)
    assert r.returncode == 0                     # not strict: the VM starts
    assert "ERROR" in r.stdout
    assert [c.split()[3] for c in host.pyrite_calls()] == ["-w"]
    assert (host.state / "pending" / "fpga.bit.failed").exists()
    assert not (host.state / "current.bit").exists()
    # a failed image is not retried at the next start
    host.calls.write_text("")
    assert host.run(HOOK, "100", "pre-start").returncode == 0
    assert host.pyrite_calls() == []


def test_hook_strict_refuses_to_start(host):
    host.configure(NATGW_STRICT="yes")
    stage(host)
    assert host.run(HOOK, "100", "pre-start", FAIL_WRITE=1).returncode == 1
    stage(host)
    assert host.run(HOOK, "100", "pre-start", FAIL_BOOT=1).returncode == 1


def test_hook_card_with_wrong_id_after_reload(host):
    host.configure(NATGW_STRICT="yes")
    stage(host)
    r = host.run(HOOK, "100", "pre-start", CARD_ID_SEEN="10ee:5000")
    assert r.returncode == 1 and "unexpected ID" in r.stdout


def test_hook_refuses_two_staged_images(host):
    stage(host, "a.bit")
    stage(host, "b.bin")
    r = host.run(HOOK, "100", "pre-start")
    assert "exactly one" in r.stdout
    assert host.pyrite_calls() == []


def test_hook_reload_on_every_start(host):
    host.configure(NATGW_RELOAD_ON_START="yes")
    assert host.run(HOOK, "100", "pre-start").returncode == 0
    assert host.pyrite_calls() == [f"pyrite -s {BDF} -b -y"]


def test_hook_rebinds_the_card_to_vfio(host):
    (host.dev / "driver").unlink()
    os.symlink(host.sysfs / "bus/pci/drivers/cndm", host.dev / "driver")
    r = host.run(HOOK, "100", "pre-start")
    # the fake sysfs cannot rebind, so the final check fails (not strict)
    assert "could not bind" in r.stdout
    assert (host.sysfs / "bus/pci/drivers/cndm/unbind").read_text().strip() == BDF
    assert (host.dev / "driver_override").read_text().strip() == "vfio-pci"
    assert (host.sysfs / "bus/pci/drivers_probe").read_text().strip() == BDF


def test_hook_missing_card(host):
    host.configure(NATGW_CARD_BDF="0000:99:00.0", NATGW_STRICT="yes")
    r = host.run(HOOK, "100", "pre-start")
    assert r.returncode == 1 and "not present" in r.stdout


def test_update_stage_replaces_and_validates(host, tmp_path):
    a = tmp_path / "one.bit"
    a.write_bytes(b"1")
    b = tmp_path / "two.bit"
    b.write_bytes(b"2")
    assert host.run(UPDATE, "--stage", str(a)).returncode == 0
    assert host.run(UPDATE, "--stage", str(b)).returncode == 0
    assert [p.name for p in (host.state / "pending").iterdir()] == ["two.bit"]
    bad = tmp_path / "x.mcs"
    bad.write_bytes(b"3")
    assert host.run(UPDATE, "--stage", str(bad)).returncode == 2


def test_update_restart_goes_through_proxmox(host):
    r = host.run(UPDATE, "--restart", "100")
    assert r.returncode == 0, r.stderr
    qm = [c for c in host.calls.read_text().splitlines() if c.startswith("qm")]
    assert qm == ["qm shutdown 100 --timeout 120", "qm start 100"]
