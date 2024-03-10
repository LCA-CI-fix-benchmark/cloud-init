# Copyright (C) 2017 Canonical Ltd.
#
# Author: Chad Smith <chad.smith@canonical.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

import abc
import glob
import logging
import os
import re
import signal
import time
from contextlib import suppress
from io import StringIO
from typing import Any, Callable, Dict, List, Optional, Tuple

import configobj
from cloudinit import subp, temp_utils, util
from cloudinit.net import (
    find_fallback_nic,
    get_devicelist,
    get_interface_mac,
    is_ib_interface,
)
)

LOG = logging.getLogger(__name__)

NETWORKD_LEASES_DIR = "/run/systemd/netif/leases"
DHCLIENT_FALLBACK_LEASE_DIR = "/var/lib/dhclient"
# Match something.lease or something.leases
DHCLIENT_FALLBACK_LEASE_REGEX = r".+\.leases?$"
UDHCPC_SCRIPT = """#!/bin/sh
log() {
    echo "udhcpc[$PPID]" "$interface: $2"
}
[ -z "$1" ] && echo "Error: should be called from udhcpc" && exit 1
case $1 in
    bound|renew)
    cat <<JSON > "$LEASE_FILE"
{
    "interface": "$interface",
    "fixed-address": "$ip",
    "subnet-mask": "$subnet",
    "routers": "${router%% *}",
    "static_routes" : "${staticroutes}"
}
JSON
    ;;
    deconfig)
    log err "Not supported"
    exit 1
    ;;
    leasefail | nak)
    log err "configuration failed: $1: $message"
    exit 1
    ;;
    *)
    echo "$0: Unknown udhcpc command: $1" >&2
    exit 1
    ;;
esac
"""


class NoDHCPLeaseError(Exception):
    """Raised when unable to get a DHCP lease."""


class InvalidDHCPLeaseFileError(NoDHCPLeaseError):
    """Raised when parsing an empty or invalid dhclient.lease file.

    Current uses are DataSourceAzure and DataSourceEc2 during ephemeral
    boot to scrape metadata.
    """


class NoDHCPLeaseInterfaceError(NoDHCPLeaseError):
    """Raised when unable to find a viable interface for DHCP."""


class NoDHCPLeaseMissingDhclientError(NoDHCPLeaseError):
    """Raised when unable to find dhclient."""


def maybe_perform_dhcp_discovery(distro, nic=None, dhcp_log_func=None):
    """Perform dhcp discovery if nic valid and dhclient command exists.

    If the nic is invalid or undiscoverable or dhclient command is not found,
    skip dhcp_discovery and return an empty dict.

    @param nic: Name of the network interface we want to run dhclient on.
    @param dhcp_log_func: A callable accepting the dhclient output and error
        streams.
    @return: A list of dicts representing dhcp options for each lease obtained
        from the dhclient discovery if run, otherwise an empty list is
        returned.
    """
    if nic is None:
        nic = find_fallback_nic()
        if nic is None:
            LOG.debug("Skip dhcp_discovery: Unable to find fallback nic.")
            raise NoDHCPLeaseInterfaceError()
    elif nic not in get_devicelist():
        LOG.debug(
            "Skip dhcp_discovery: nic %s not found in get_devicelist.", nic
        )
        raise NoDHCPLeaseInterfaceError()
    return distro.dhcp_client.dhcp_discovery(nic, dhcp_log_func, distro)


def networkd_parse_lease(content):
    """Parse a systemd lease file content as in /run/systemd/netif/leases/

    Parse this (almost) ini style file even though it says:
      # This is private data. Do not parse.

    Simply return a dictionary of key/values."""

    return dict(configobj.ConfigObj(StringIO(content), list_values=False))


def networkd_load_leases(leases_d=None):
    """Return a dictionary of dictionaries representing each lease
    found in lease_d.i

    The top level key will be the filename, which is typically the ifindex."""

    if leases_d is None:
        leases_d = NETWORKD_LEASES_DIR

    ret = {}
    if not os.path.isdir(leases_d):
        return ret
    for lfile in os.listdir(leases_d):
        ret[lfile] = networkd_parse_lease(
            util.load_file(os.path.join(leases_d, lfile))
        )
    return ret


def networkd_get_option_from_leases(keyname, leases_d=None):
    if leases_d is None:
        leases_d = NETWORKD_LEASES_DIR
    leases = networkd_load_leases(leases_d=leases_d)
    for _ifindex, data in sorted(leases.items()):
        if data.get(keyname):
            return data[keyname]
    return None


class DhcpClient(abc.ABC):
    client_name = ""
    max_wait = 5

    def __init__(self):
        self.dhcp_client_path = subp.which(self.client_name)
        if not self.dhcp_client_path:
            raise NoDHCPLeaseMissingDhclientError()

    @classmethod
    def kill_dhcp_client(cls):
        subp.subp(["pkill", cls.client_name], rcs=[0, 1])

    @classmethod
    def clear_leases(cls):
        cls.kill_dhcp_client()
        files = glob.glob("/var/lib/dhcp/*")
        for file in files:
            os.remove(file)

    @classmethod
    def start_service(cls, dhcp_interface: str, distro):
        distro.manage_service(
            "start", cls.client_name, dhcp_interface, rcs=[0, 1]
        )

    @classmethod
    def stop_service(cls, dhcp_interface: str, distro):
        distro.manage_service("stop", cls.client_name, rcs=[0, 1])

    @staticmethod
    def parse_static_routes(routes: str) -> List[Tuple[str, str]]:
        return []

    def get_newest_lease(self, distro) -> Dict[str, Any]:
        return {}


class IscDhclient(DhcpClient):
    client_name = "dhclient"

    def __init__(self):
        super().__init__()
        self.lease_file = "/run/dhclient.lease"

    @staticmethod
    def parse_leases(lease_content: str) -> List[Dict[str, Any]]:
        """parse the content of a lease file

        @param lease_content: a string containing the contents of an
                              isc-dhclient lease
        @return: a list of leases, most recent last
        """
        lease_regex = re.compile(r"lease {(?P<lease>.*?)}\n", re.DOTALL)
        dhcp_leases: List[Dict] = []
        if len(lease_content) == 0:
            return []
        for lease in lease_regex.findall(lease_content):
            lease_options = []
            for line in lease.split(";"):
                # Strip newlines, double-quotes and option prefix
                line = line.strip().replace('"', "").replace("option ", "")
                if line:
                    lease_options.append(line.split(" ", 1))
            dhcp_leases.append(dict(lease_options))
        return dhcp_leases

    def get_newest_lease(self, distro) -> Dict[str, Any]:
        """Get the most recent lease from the ephemeral phase as a dict.

        Return a dict of dhcp options. The dict contains key value
        pairs from the most recent lease.

        @param distro: a distro object - not used in this class, but required
                       for function signature compatibility with other classes
                       that require a distro object
        @raises: InvalidDHCPLeaseFileError on empty or unparseable leasefile
            content.
        """
        with suppress(FileNotFoundError):
            content: str
            content = util.load_file(self.lease_file)  # pyright: ignore
            if content:
                dhcp_leases = self.parse_leases(content)
                if dhcp_leases:
                    return dhcp_leases[-1]
        return {}

    def dhcp_discovery(
        self,
        interface: str,
        dhcp_log_func: Optional[Callable] = None,
        distro=None,
    ) -> Dict[str, Any]:
        """Run dhclient on the interface without scripts/filesystem artifacts.

        @param interface: Name of the network interface on which to send a
                          dhcp request
        @param dhcp_log_func: A callable accepting the dhclient output and
                              error streams.
        @param distro: a distro object for network interface manipulation
        @return: dict of lease options representing the most recent dhcp lease
                 parsed from the dhclient.lease file
        """
        LOG.debug("Performing a dhcp discovery on %s", interface)

        # We want to avoid running /sbin/dhclient-script because of
        # side-effects in # /etc/resolv.conf any any other vendor specific
        # scripts in /etc/dhcp/dhclient*hooks.d.
        pid_file = "/run/dhclient.pid"
        config_file = None

        # this function waits for these files to exist, clean previous runs
        # to avoid false positive in wait_for_files
        with suppress(FileNotFoundError):
            os.remove(pid_file)
            os.remove(self.lease_file)

        # ISC dhclient needs the interface up to send initial discovery packets
        # Generally dhclient relies on dhclient-script PREINIT action to bring
        # the link up before attempting discovery. Since we are using
        # -sf /bin/true, we need to do that "link up" ourselves first.
        distro.net_ops.link_up(interface)
        # For INFINIBAND port the dhlient must be sent with
        # dhcp-client-identifier. So here we are checking if the interface is
        # INFINIBAND or not. If yes, we are generating the the client-id to be
        # used with the dhclient
        if is_ib_interface(interface):
            dhcp_client_identifier = (
                "20:%s" % get_interface_mac(interface)[36:]
            )
            interface_dhclient_content = (
                'interface "%s" '
                "{send dhcp-client-identifier %s;}"
                % (interface, dhcp_client_identifier)
            )
            tmp_dir = temp_utils.get_tmp_ancestor(needs_exe=True)
            config_file = os.path.join(tmp_dir, interface + "-dhclient.conf")
            util.write_file(config_file, interface_dhclient_content)

        try:
            out, err = subp.subp(
                distro.build_dhclient_cmd(
                    self.dhcp_client_path,
                    self.lease_file,
                    pid_file,
                    interface,
                    config_file,
                )
            )
        except subp.ProcessExecutionError as error:
            LOG.debug(
                "dhclient exited with code: %s stderr: %r stdout: %r",
                error.exit_code,
                error.stderr,
                error.stdout,
            )
            raise NoDHCPLeaseError from error

        # Wait for pid file and lease file to appear, and for the process
        # named by the pid file to daemonize (have pid 1 as its parent). If we
        # try to read the lease file before daemonization happens, we might try
        # to read it before the dhclient has actually written it. We also have
        # to wait until the dhclient has become a daemon so we can be sure to
        # kill the correct process, thus freeing cleandir to be deleted back
        # up the callstack.
        missing = util.wait_for_files(
            [pid_file, self.lease_file], maxwait=self.max_wait, naplen=0.01
        )
        if missing:
            LOG.warning(
                "dhclient did not produce expected files: %s",
                ", ".join(os.path.basename(f) for f in missing),
            )
            return {}

        ppid = "unknown"
        daemonized = False
        pid_content = None
        for _ in range(1000):
            pid_content = util.load_file(pid_file).strip()
            try:
                pid = int(pid_content)
            except ValueError:
                pass
            else:
                ppid = util.get_proc_ppid(pid)
                if ppid == 1:
                    LOG.debug("killing dhclient with pid=%s", pid)
                    os.kill(pid, signal.SIGKILL)
                    daemonized = True
                    break
            time.sleep(0.01)

        if not daemonized:
            LOG.error(
                "dhclient(pid=%s, parentpid=%s) failed to daemonize after %s "
                "seconds",
                pid_content,
                ppid,
                0.01 * 1000,
            )
        if dhcp_log_func is not None:
            dhcp_log_func(out, err)
        lease = self.get_newest_lease(distro)
        if lease:
            return lease
        raise InvalidDHCPLeaseFileError()

    @staticmethod
    def parse_static_routes(routes: str) -> List[Tuple[str, str]]:
        """
        parse rfc3442 format and return a list containing tuple of strings.

        The tuple is composed of the network_address (including net length) and
        gateway for a parsed static route.  It can parse two formats of
        rfc3442, one from dhcpcd and one from dhclient (isc).

        @param rfc3442: string in rfc3442 format (isc or dhcpd)
        @returns: list of tuple(str, str) for all valid parsed routes until the
                  first parsing error.

        e.g.:

        sr=parse_static_routes(\
        "32,169,254,169,254,130,56,248,255,0,130,56,240,1")
        sr=[
            ("169.254.169.254/32", "130.56.248.255"), \
        ("0.0.0.0/0", "130.56.240.1")
        ]

        sr2 = parse_static_routes(\
        "24.191.168.128 192.168.128.1,0 192.168.128.1")
        sr2 = [
            ("191.168.128.0/24", "192.168.128.1"),\
        ("0.0.0.0/0", "192.168.128.1")
        ]

        Python version of isc-dhclient's hooks:
           /etc/dhcp/dhclient-exit-hooks.d/rfc3442-classless-routes
        """
        # raw strings from dhcp lease may end in semi-colon
        rfc3442 = routes.rstrip(";")
        tokens = [tok for tok in re.split(r"[, .]", rfc3442) if tok]
        static_routes: List[Tuple[str, str]] = []

        def _trunc_error(cidr, required, remain):
            msg = (
                "RFC3442 string malformed.  Current route has CIDR of %s "
                "and requires %s significant octets, but only %s remain. "
                "Verify DHCP rfc3442-classless-static-routes value: %s"
                % (cidr, required, remain, rfc3442)
            )
            LOG.error(msg)

        current_idx = 0
        for idx, tok in enumerate(tokens):
            if idx < current_idx:
                continue
            net_length = int(tok)
            if net_length in range(25, 33):
                req_toks = 9
                if len(tokens[idx:]) < req_toks:
                    _trunc_error(net_length, req_toks, len(tokens[idx:]))
                    return static_routes
                net_address = ".".join(tokens[idx + 1 : idx + 5])
                gateway = ".".join(tokens[idx + 5 : idx + req_toks])
                current_idx = idx + req_toks
            elif net_length in range(17, 25):
                req_toks = 8
                if len(tokens[idx:]) < req_toks:
                    _trunc_error(net_length, req_toks, len(tokens[idx:]))
                    return static_routes
                net_address = ".".join(tokens[idx + 1 : idx + 4] + ["0"])
                gateway = ".".join(tokens[idx + 4 : idx + req_toks])
                current_idx = idx + req_toks
            elif net_length in range(9, 17):
                req_toks = 7
                if len(tokens[idx:]) < req_toks:
                    _trunc_error(net_length, req_toks, len(tokens[idx:]))
                    return static_routes
                net_address = ".".join(tokens[idx + 1 : idx + 3] + ["0", "0"])
                gateway = ".".join(tokens[idx + 3 : idx + req_toks])
                current_idx = idx + req_toks
            elif net_length in range(1, 9):
                req_toks = 6
                if len(tokens[idx:]) < req_toks:
                    _trunc_error(net_length, req_toks, len(tokens[idx:]))
                    return static_routes
                net_address = ".".join(
                    tokens[idx + 1 : idx + 2] + ["0", "0", "0"]
                )
                gateway = ".".join(tokens[idx + 2 : idx + req_toks])
                current_idx = idx + req_toks
            elif net_length == 0:
                req_toks = 5
                if len(tokens[idx:]) < req_toks:
                    _trunc_error(net_length, req_toks, len(tokens[idx:]))
                    return static_routes
                net_address = "0.0.0.0"
                gateway = ".".join(tokens[idx + 1 : idx + req_toks])
                current_idx = idx + req_toks
            else:
                LOG.error(
                    'Parsed invalid net length "%s".  Verify DHCP '
                    "rfc3442-classless-static-routes value.",
                    net_length,
                )
                return static_routes

            static_routes.append(
                ("%s/%s" % (net_address, net_length), gateway)
            )

        return static_routes

    @staticmethod
    def get_newest_lease_file_from_distro(distro) -> Optional[str]:
        """Get the latest lease file from a distro-managed dhclient

        Doesn't consider the ephemeral timeframe lease.

        @param distro: used for distro-specific lease location and filename
        @return: The most recent lease file, or None
        """
        latest_file = None

        # Try primary dir/regex, then the fallback ones
        for directory, regex in (
            (
                distro.dhclient_lease_directory,
                distro.dhclient_lease_file_regex,
            ),
            (DHCLIENT_FALLBACK_LEASE_DIR, DHCLIENT_FALLBACK_LEASE_REGEX),
        ):
            if not directory:
                continue

            lease_files = []
            try:
                lease_files = os.listdir(directory)
            except FileNotFoundError:
                continue

            latest_mtime = -1.0
            for fname in lease_files:
                if not re.search(regex, fname):
                    continue

                abs_path = os.path.join(directory, fname)
                mtime = os.path.getmtime(abs_path)
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    latest_file = abs_path

            # Lease file found, skipping falling back
            if latest_file:
                return latest_file
        return None

    def get_key_from_latest_lease(self, distro, key: str):
        """Get a key from the latest lease from distro-managed dhclient

        Doesn't consider the ephemeral timeframe lease.

        @param lease_dir: distro-specific lease to check
        @param lease_file_regex: distro-specific regex to match lease name
        @return: The most recent lease file, or None
        """
        lease_file = self.get_newest_lease_file_from_distro(distro)
        if lease_file:
            with suppress(FileNotFoundError):
                content: str
                content = util.load_file(lease_file)  # pyright: ignore
                if content:
                    for lease in reversed(self.parse_leases(content)):
                        server = lease.get(key)
                        if server:
                            return server


class Dhcpcd(DhcpClient):
    client_name = "dhcpcd"

    def __init__(self):
        super().__init__()

    def dhcp_discovery(
        self,
        interface: str,
        dhcp_log_func: Optional[Callable] = None,
        distro=None,
    ) -> Dict[str, Any]:
        """Run dhcpcd on the interface without scripts/filesystem artifacts.

        @param interface: Name of the network interface on which to send a
                          dhcp request
        @param dhcp_log_func: A callable accepting the client output and
                              error streams.
        @param distro: a distro object for network interface manipulation
        @return: dict of lease options representing the most recent dhcp lease
                 parsed from the dhclient.lease file
        """
        LOG.debug("Performing a dhcp discovery on %s", interface)

        # ISC dhclient needs the interface up to send initial discovery packets
        # Generally dhclient relies on dhclient-script PREINIT action to bring
        # the link up before attempting discovery. Since we are using
        # -sf /bin/true, we need to do that "link up" ourselves first.
        distro.net_ops.link_up(interface)

        # TODO: disabling hooks means we need to get all of the files in
        # /lib/dhcpcd/dhcpcd-hooks/ and pass each of those with the --nohook
        # argument to dhcpcd
        try:
            out, err = subp.subp(
                [
                    self.client_name,
                    "--oneshot",  # get lease then exit
                    "--nobackground",  # don't fork
                    "--ipv4only",  # only attempt configuring ipv4
                    "--waitip=4",  # wait for ipv4 to be configured
                    "--persistent",  # don't deconfigure when dhcpcd exits
                    "--noarp",  # don't be slow
                    interface,
                ]
            )
            if dhcp_log_func is not None:
                dhcp_log_func(out, err)
            lease = self.get_newest_lease(distro)
            if lease:
                return lease
            raise NoDHCPLeaseError("No lease found")

        except subp.ProcessExecutionError as error:
            LOG.debug(
                "dhclient exited with code: %s stderr: %r stdout: %r",
                error.exit_code,
                error.stderr,
                error.stdout,
            )
            raise NoDHCPLeaseError from error

    @staticmethod
    def parse_dhcpcd_lease(lease_dump: str, interface: str) -> Dict:
        """parse the output of dhcpcd --dump

        map names to the datastructure we create from dhclient

        example dhcpcd output:

        broadcast_address='192.168.15.255'
        dhcp_lease_time='3600'
        dhcp_message_type='5'
        dhcp_server_identifier='192.168.0.1'
        domain_name='us-east-2.compute.internal'
        domain_name_servers='192.168.0.2'
        host_name='ip-192-168-0-212'
        interface_mtu='9001'
        ip_address='192.168.0.212'
        network_number='192.168.0.0'
        routers='192.168.0.1'
        subnet_cidr='20'
        subnet_mask='255.255.240.0'
        """

        # create a dict from dhcpcd dump output - remove single quotes
        lease = dict(
            [
                a.split("=")
                for a in lease_dump.strip().replace("'", "").split("\n")
            ]
        )

        # this is expected by cloud-init's code
        lease["interface"] = interface

        # transform underscores to hyphens
        lease = {key.replace("_", "-"): value for key, value in lease.items()}

        # map names that are different
        name_map = {
            "ip-address": "fixed-address",
            "classless-static-routes": "static_routes",
        }
        for source, destination in name_map.items():
            if source in lease:
                lease[destination] = lease.pop(source)

        # nothing ever uses multiple leases in cloud-init codebase but
        # for compatibility we still expect a list of leases
        return lease

    def get_newest_lease(self, distro) -> Dict[str, Any]:
        """Return a lease

        Return a list of dicts of dhcp options. Each dict contains key value
        pairs a specific lease in order from oldest to newest.

        @raises: InvalidDHCPLeaseFileError on empty or unparseable leasefile
            content.
        """
        try:
            return self.parse_dhcpcd_lease(
                subp.subp(
                    [
                        "dhcpcd",
                        "--dumplease",
                        distro.fallback_interface,
                    ],
                    rcs=[0, 1],
                ).stdout,
                distro.fallback_interface,
            )

        except subp.ProcessExecutionError as error:
            LOG.debug(
                "dhcpcd exited with code: %s stderr: %r stdout: %r",
                error.exit_code,
                error.stderr,
                error.stdout,
            )
            raise NoDHCPLeaseError from error

    @staticmethod
    def parse_static_routes(routes: str) -> List[Tuple[str, str]]:
        """
        classless static routes as returnedd from dhcpcd --dumplease and return
        a list containing tuple of strings.

        The tuple is composed of the network_address (including net length) and
        gateway for a parsed static route.

        @param routes: string containing classless static routes
        @returns: list of tuple(str, str) for all valid parsed routes until the
                  first parsing error.

        e.g.:

        sr=parse_static_routes(
            "0.0.0.0/0 10.0.0.1 168.63.129.16/32 10.0.0.1"
        )
        sr=[
            ("0.0.0.0/0", "10.0.0.1"),
            ("169.63.129.16/32", "10.0.0.1"),
        ]
        """
        static_routes = routes.split()
        if static_routes:
            # format: dest1/mask gw1 ... destn/mask gwn
            return [i for i in zip(static_routes[::2], static_routes[1::2])]
        LOG.warning("Malformed classless static routes: [%s]", routes)
        return []


class Udhcpc(DhcpClient):
    client_name = "udhcpc"

    def __init__(self):
        super().__init__()
        self.lease_file = None

    def dhcp_discovery(
        self,
        interface: str,
        dhcp_log_func: Optional[Callable] = None,
        distro=None,
    ) -> Dict[str, Any]:
        """Run udhcpc on the interface without scripts or filesystem artifacts.

        @param interface: Name of the network interface on which to run udhcpc.
        @param dhcp_log_func: A callable accepting the udhcpc output and
                              error streams.
        @return: A list of dicts of representing the dhcp leases parsed from
                 the udhcpc lease file.
        """
        LOG.debug("Performing a dhcp discovery on %s", interface)

        tmp_dir = temp_utils.get_tmp_ancestor(needs_exe=True)
        self.lease_file = os.path.join(tmp_dir, interface + ".lease.json")
        with suppress(FileNotFoundError):
            os.remove(self.lease_file)

        # udhcpc needs the interface up to send initial discovery packets
        distro.net_ops.link_up(interface)

        udhcpc_script = os.path.join(tmp_dir, "udhcpc_script")
        util.write_file(udhcpc_script, UDHCPC_SCRIPT, 0o755)

        cmd = [
            self.dhcp_client_path,
            "-O",
            "staticroutes",
            "-i",
            interface,
            "-s",
            udhcpc_script,
            "-n",  # Exit if lease is not obtained
            "-q",  # Exit after obtaining lease
            "-f",  # Run in foreground
            "-v",
        ]

        # For INFINIBAND port the dhcpc must be running with
        # client id option. So here we are checking if the interface is
        # INFINIBAND or not. If yes, we are generating the the client-id to be
        # used with the udhcpc
        if is_ib_interface(interface):
            dhcp_client_identifier = get_ib_interface_hwaddr(
                interface, ethernet_format=True
            )
            cmd.extend(
                ["-x", "0x3d:%s" % dhcp_client_identifier.replace(":", "")]
            )
        try:
            out, err = subp.subp(
                cmd, update_env={"LEASE_FILE": self.lease_file}, capture=True
            )
        except subp.ProcessExecutionError as error:
            LOG.debug(
                "udhcpc exited with code: %s stderr: %r stdout: %r",
                error.exit_code,
                error.stderr,
                error.stdout,
            )
            raise NoDHCPLeaseError from error

        if dhcp_log_func is not None:
            dhcp_log_func(out, err)

        return self.get_newest_lease(distro)

    def get_newest_lease(self, distro) -> Dict[str, Any]:
        """Get the most recent lease from the ephemeral phase as a dict.

        Return a dict of dhcp options. The dict contains key value
        pairs from the most recent lease.

        @param distro: a distro object - not used in this class, but required
                       for function signature compatibility with other classes
                       that require a distro object
        @raises: InvalidDHCPLeaseFileError on empty or unparseable leasefile
            content.
        """
        lease_json = util.load_json(util.load_file(self.lease_file))
        static_routes = lease_json["static_routes"].split()
        if static_routes:
            # format: dest1/mask gw1 ... destn/mask gwn
            lease_json["static_routes"] = [
                i for i in zip(static_routes[::2], static_routes[1::2])
            ]
        return lease_json
