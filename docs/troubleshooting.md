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

To report wrong or missing values or connection problems:

1. Open the integration's options (**Configure**) and turn on **Enable packet
   collection** under **Diagnostics**.
2. Reproduce the problem, or wait a minute.
3. Download the diagnostics from the device page and attach the file to your issue.
4. Turn packet collection off again.

The download shows only the first half of the UPS's addresses, e.g. `0A:1B:2C:**:**:**`.
It stores the exchanged data decrypted, but without the session key material and without
the Wi-Fi network name and IP addresses, so nothing in it reveals the rest of the UPS's
address.

## Logs

For connection problems, open the integration's options (**Configure**) and turn on
**Log device connection details** under **Logging Options**. The other logging options
produce a lot of output, so turn them off again afterwards. **Mask sensitive
information** masks the device's address in the log.

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
