"""The Azure VM Duck backend (plans/azure-gcp.md, phase 3) against a fake Compute client: the VM's shape, its
boot script, and teardown (a stopped Azure VM is still billed, so the Catchment deletes it)."""

from __future__ import annotations

import base64
import types

import pytest

from duckstring.catchment.azurevm_launcher import AzureVmLauncher, _image_reference
from duckstring.catchment.dialback import RemoteDialback
from duckstring.catchment.vm_boot import boot_script

pytestmark = pytest.mark.timeout(10)

SUB = "00000000-0000-0000-0000-000000000001"
SUBNET = (f"/subscriptions/{SUB}/resourceGroups/net-rg/providers/Microsoft.Network/virtualNetworks/v/subnets/ducks")
IDN = f"/subscriptions/{SUB}/resourceGroups/lake-rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/duck"


class _NotFound(Exception):
    pass


class _Vms:
    def __init__(self):
        self.calls, self.state, self.existing = [], {}, []

    def begin_create_or_update(self, rg, name, body):
        self.calls.append(("create", rg, name, body))
        self.state[name] = "PowerState/starting"

    def begin_delete(self, rg, name):
        self.calls.append(("delete", rg, name))
        self.state.pop(name, None)

    def instance_view(self, rg, name):
        if name not in self.state:
            raise _NotFound("(ResourceNotFound) The Resource was not found.")
        return types.SimpleNamespace(statuses=[
            types.SimpleNamespace(code="ProvisioningState/succeeded", message=None),
            types.SimpleNamespace(code=self.state[name], message=None)])

    def list(self, rg):
        self.calls.append(("list", rg))
        return self.existing

    def retrieve_boot_diagnostics_data(self, rg, name):
        return types.SimpleNamespace(serial_console_log_blob_uri=None)


def _launcher(**kw):
    vms = _Vms()
    client = types.SimpleNamespace(virtual_machines=vms)
    kw.setdefault("subnet", SUBNET)
    kw.setdefault("identity", IDN)
    kw.setdefault("location", "australiaeast")
    la = AzureVmLauncher("/tmp/x", "http://cat:7474", token="tok", data_root="abfss://c@a.dfs.core.windows.net/d",
                         dialback=RemoteDialback("http://cat:7474"), client=client, pip_spec="duckstring[azure]==9.9",
                         reap_poll_s=0.01, **kw)
    return la, vms


def _created(vms):
    return [c for c in vms.calls if c[0] == "create"]


def test_a_duck_is_a_vm_on_the_operators_subnet_with_its_identity():
    la, vms = _launcher()
    la.ensure("sales@1", "1.0.0", "ponds/sales/1.0.0", duck={"pool": {"instance_type": "Standard_E8s_v5"}})
    [(_, rg, name, body)] = _created(vms)
    assert rg == "net-rg" and name.startswith("duck-sales-1-") and len(name) <= 64
    props = body["properties"]
    assert body["location"] == "australiaeast" and props["hardwareProfile"] == {"vmSize": "Standard_E8s_v5"}
    assert body["identity"] == {"type": "UserAssigned", "userAssignedIdentities": {IDN: {}}}
    assert body["tags"]["duckstring-catchment"] == la.catchment_tag
    nic = props["networkProfile"]["networkInterfaceConfigurations"][0]["properties"]
    ip = nic["ipConfigurations"][0]["properties"]
    assert nic["deleteOption"] == "Delete" and ip["subnet"] == {"id": SUBNET}
    assert ip["publicIPAddressConfiguration"]["properties"]["deleteOption"] == "Delete"
    assert props["storageProfile"]["osDisk"]["deleteOption"] == "Delete"
    assert props["storageProfile"]["imageReference"] == {
        "publisher": "Canonical", "offer": "ubuntu-24_04-lts", "sku": "server", "version": "latest"}
    os_profile = props["osProfile"]
    assert os_profile["linuxConfiguration"]["disablePasswordAuthentication"] is True
    assert os_profile["linuxConfiguration"]["ssh"]["publicKeys"][0]["keyData"].startswith("ssh-rsa ")
    script = base64.b64decode(os_profile["customData"]).decode()
    assert "uv pip install --python /opt/duckstring/bin/python 'duckstring[azure]==9.9'" in script
    assert "-m duckstring.duck --pond sales --major 1" in script and "--token=tok" in script
    assert script.rstrip().endswith("&& shutdown -h now") and props["diagnosticsProfile"]["bootDiagnostics"]["enabled"]
    assert la.is_running("sales@1")


def test_the_deploy_config_places_and_sizes_the_vm():
    la, vms = _launcher()
    la.ensure("sales@1", "1.0.0", "p", duck={"deploy_config": {
        "resource_group": "ducks-rg", "assign_public_ip": "false", "disk_gb": 256,
        "image": "/subscriptions/x/resourceGroups/g/providers/Microsoft.Compute/galleries/g/images/duck/versions/1"},
        "dedicated_instance_type": "Standard_NC6s_v3"})
    [(_, rg, _, body)] = _created(vms)
    props = body["properties"]
    assert rg == "ducks-rg" and props["hardwareProfile"]["vmSize"] == "Standard_NC6s_v3"
    ip = props["networkProfile"]["networkInterfaceConfigurations"][0]["properties"]["ipConfigurations"][0]["properties"]
    assert "publicIPAddressConfiguration" not in ip
    assert props["storageProfile"]["osDisk"]["diskSizeGB"] == 256
    assert props["storageProfile"]["imageReference"]["id"].endswith("/versions/1")


def test_missing_placement_fails_the_launch():
    la, vms = _launcher(subnet="", location="")
    la.ensure("sales@1", "1.0.0", "p", duck={})
    assert "DUCKSTRING_AZUREVM_SUBNET" in la.launch_error("sales@1") and vms.calls == []


def test_terminate_deletes_and_release_deletes_once_the_duck_has_exited():
    la, vms = _launcher()
    la.ensure("sales@1", "1.0.0", "p", duck={})
    la.ensure("reports@1", "1.0.0", "p", duck={})
    sales, reports = (c[2] for c in _created(vms))
    la.terminate("sales@1")
    assert ("delete", "net-rg", sales) in vms.calls and not la.is_running("sales@1")
    vms.state[reports] = "PowerState/running"
    la.release("reports@1")
    assert not la.is_running("reports@1")
    import time

    time.sleep(0.05)
    assert ("delete", "net-rg", reports) not in vms.calls  # still running: its Duck is persisting
    vms.state[reports] = "PowerState/stopped"  # the Duck exited and powered the OS off
    for _ in range(100):
        if ("delete", "net-rg", reports) in vms.calls:
            break
        time.sleep(0.01)
    assert ("delete", "net-rg", reports) in vms.calls


def test_a_launch_first_sweeps_this_catchments_stopped_vms():
    la, vms = _launcher()
    mine = types.SimpleNamespace(name="duck-old-1", tags={"duckstring-catchment": la.catchment_tag})
    theirs = types.SimpleNamespace(name="duck-other-1", tags={"duckstring-catchment": "someone-else"})
    running = types.SimpleNamespace(name="duck-live-1", tags={"duckstring-catchment": la.catchment_tag})
    vms.existing = [mine, theirs, running]
    vms.state.update({"duck-old-1": "PowerState/stopped", "duck-other-1": "PowerState/stopped",
                      "duck-live-1": "PowerState/running"})
    la.ensure("sales@1", "1.0.0", "p", duck={})
    la.ensure("reports@1", "1.0.0", "p", duck={})
    deleted = [c[2] for c in vms.calls if c[0] == "delete"]
    assert deleted == ["duck-old-1"] and [c for c in vms.calls if c[0] == "list"] == [("list", "net-rg")]


def test_diagnose_reports_the_vms_statuses():
    la, vms = _launcher()
    la.ensure("sales@1", "1.0.0", "p", duck={})
    assert "PowerState/starting" in la.diagnose("sales@1")


def test_the_pool_machine_is_one_vm_running_the_agent():
    from duckstring.catchment.pool_launcher import VmPoolMachine

    la, vms = _launcher()
    m = VmPoolMachine("heavy", {"instance_type": "Standard_D16s_v5"}, la)
    m.start()
    [(_, _, name, body)] = _created(vms)
    script = base64.b64decode(body["properties"]["osProfile"]["customData"]).decode()
    assert name.startswith("duck-pool-heavy-") and "-m duckstring.duck.pool_agent --pool heavy" in script
    assert body["tags"]["duckstring"] == "pool-agent" and m.is_up()
    m._probed_at = 0
    assert m.check()
    vms.state[name] = "PowerState/deallocated"
    m._probed_at = 0
    assert not m.check() and "disappeared" in m.error()
    m.start()
    m.stop()
    assert sum(c[0] == "delete" for c in vms.calls) == 1 and not m.is_up()


def test_images_are_urns_or_resource_ids():
    assert _image_reference("Canonical:ubuntu-24_04-lts:server") == {
        "publisher": "Canonical", "offer": "ubuntu-24_04-lts", "sku": "server", "version": "latest"}
    assert _image_reference("/subscriptions/s/x") == {"id": "/subscriptions/s/x"}


def test_the_boot_script_quotes_its_arguments():
    script = boot_script(["duckstring.duck", "--token=a b'c"], "duckstring[azure]==1.0")
    assert "'--token=a b'\"'\"'c'" in script
