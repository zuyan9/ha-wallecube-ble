# Troubleshooting

## The WalleCube app can't find the UPS

The UPS accepts only one Bluetooth connection at a time and stops advertising while one
is open. While Home Assistant is connected, the WalleCube app and its WeChat mini program
can't find the UPS and report a scan timeout. To use the app's Bluetooth functions, such
as the advanced configuration, Wi-Fi setup or the Wake-on-LAN targets:

1. Disable the UPS's entry under **Settings → Devices & services → WalleCube BLE**.
2. Use the app.
3. Leave the UPS's page in the app, or close the app or mini program completely, so it
   releases the connection.
4. Enable the entry again.

The app's cloud functions are not affected.

## The UPS is unavailable

- **The entry shows "Retrying setup"**: Home Assistant can't connect, for example
  because the app holds the connection or the UPS is out of range. It keeps retrying, at
  least every 10 minutes, and when it connects through an ESPHome Bluetooth proxy it
  often reconnects as soon as the UPS advertises again.
- **Only the measured values are unavailable**: the UPS is connected but has sent no
  measurements for a minute, for example because it turned its output off on battery or
  updates its firmware. They return with the next measurement.

## Diagnostics

To report wrong or missing values or connection problems, download the diagnostics from
the device page right after the problem occurred and attach the file to your issue. The
download contains the connection history and the last 200 messages exchanged with the
UPS in each direction, about three and a half minutes of measurements. Both start over
after a dropped connection.

The download shows only the first half of the UPS's addresses, e.g. `0A:1B:2C:**:**:**`.
It stores the exchanged data decrypted, but without the session key material and without
the Wi-Fi network name and IP addresses, so nothing in it reveals the rest of the UPS's
address.

## Logs

For connection problems, open the integration under **Settings → Devices & services**,
select **Enable debug logging** in its **⋮** menu, reproduce the problem and select
**Disable debug logging**. Home Assistant then downloads the log. For more detail from
the Bluetooth stack, do the same for the **Bluetooth** integration.

Before the UPS is set up, the integration has no menu yet: use the `logger.set_level`
action with `custom_components.wallecube_ble: debug` instead. The same action with
`bleak: debug` adds the messages of bleak itself, which the Bluetooth integration's debug
logging leaves out.

The integration's log lines name the UPS by the last four digits of its address, like
its default name (e.g. `W150-4E52`), and connection errors add the first half of the
address, e.g. `0A:1B:2C:**:**:**`, so together they hide only one of its six parts.
Other lines, especially those of the Bluetooth libraries, can contain the full address,
also written with underscores (`dev_0A_1B_…`), and the advertised name `Walle-…`, which
contains the factory MAC. Replace them before posting a log.

## Security

The UPS needs no pairing: the session key is derived from the MAC address the UPS
advertises and a secret that is the same in every unit and is published in the vendor
apps and other tools, including this integration. Anyone within Bluetooth range can
therefore read and change the UPS's settings while no other client is connected.

That includes Adapter Voltage, which is also the voltage the UPS outputs on battery and
sets the input voltage it accepts as power. Wrong adapter settings can stop the battery
from charging, make the UPS treat a working adapter as lost or change its output voltage
on battery. The power board applies them when it next restarts.

While Home Assistant is connected, the UPS accepts no other connection, so keeping the
integration connected also keeps others out.

Without Wi-Fi credentials, the UPS keeps trying, about every 10 s, to join a network
that is built into its firmware. Wi-Fi can't be turned off over Bluetooth. To control
which network the UPS uses, set up its Wi-Fi with the vendor app.
