import ipaddress
from typing import Annotated

from .base import RawData


class WifiStatus(RawData):
    """
    Fixed part of the Wi-Fi status reply on the configuration channel

    The network name follows with `ssid_length` bytes. While the device is not
    connected, the firmware zeroes the fields but leaves `ssid_length` uninitialized.
    """

    connected: Annotated[int, "B"]
    rssi: Annotated[int, "b"]  # dBm
    ip_address: Annotated[bytes, "4s"]
    gateway: Annotated[bytes, "4s"]
    netmask: Annotated[bytes, "4s"]
    ssid_length: Annotated[int, "B"]


def ipv4(packed: bytes | None) -> str | None:
    """Dotted form of an address in network byte order, None if it is unset"""
    if packed is None or len(packed) != 4 or packed == bytes(4):
        return None
    return str(ipaddress.IPv4Address(packed))
