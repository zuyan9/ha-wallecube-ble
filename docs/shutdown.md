# Shutting down a NAS or computer

When input power fails, the UPS powers its output from the battery until it decides to
turn the output off. This page explains how to shut equipment down in time, and how much
time the UPS leaves. There are two ways to trigger the shutdown: the UPS's USB port and
the entities of this integration. Use the USB port where you can.

## Over USB (recommended)

The USB port on the back of the UPS presents it to a connected computer as a standard
USB UPS, with the USB ID `04d8:d005` (shown as "Smart UPS W150" on a W150). The ID is
that of Mini-Box's openUPS2, which the `usbhid-ups` driver of
[Network UPS Tools (NUT)](https://networkupstools.org/) supports. Most NAS systems use NUT
for their USB UPS support, among them Synology DSM, QNAP QTS and TrueNAS; Unraid and
Proxmox can add it.

- Connect the UPS's USB port to the NAS or server and turn on its USB UPS support.
- This needs neither Home Assistant, Bluetooth nor a network, and it works alongside
  this integration.
- Prefer the NAS's option to shut down after a few minutes on battery. The UPS's
  low-battery signal over USB is the same condition as Shutdown Imminent and leaves only
  a few minutes, see [How much time Shutdown Imminent leaves](#how-much-time-shutdown-imminent-leaves).
- The UPS can't be told over USB to turn its output off, so it keeps powering the NAS
  after the NAS has shut down. When input power returns, the NAS stays off unless the
  outage lasted until the UPS turned its output off. A UPS on Wi-Fi can wake it with
  Wake-on-LAN, see Auto Boot in [Controls](entities.md#controls).
- With power-board firmware older than 1.29, NUT shows a wrong battery temperature and
  battery current; the status and the charge are right. With 1.29, the UPS also signals
  a USB wake-up when input power returns, which wakes a computer that is asleep and
  allows waking from USB.
- NUT reports the front panel's MAC address as the UPS's serial number. Mask it before
  you post NUT output publicly.

> [!NOTE]
> The USB ID has been confirmed on a W150, but a complete shutdown through NUT has not
> been tested yet. Please [report](https://github.com/zuyan9/ha-wallecube-ble/issues/new/choose)
> how it works on your system.

## With Home Assistant

An automation can shut equipment down as well, but only if Home Assistant, its Bluetooth
adapter or proxy and the network path to the equipment stay powered during the outage,
that is, are on the UPS too.

- **Input Power** turning off for a few minutes is the main trigger. It turns off when
  the input drops below the power-good threshold, by default 95.8 % of Adapter Voltage.
- **Shutdown Imminent** turning on is the last resort, see
  [How much time Shutdown Imminent leaves](#how-much-time-shutdown-imminent-leaves).
- **Battery Level** works with a margin. With power-board firmware 1.29 it counts only
  the charge above the reserve the UPS keeps, so the output turns off at 0 to 1 %; with
  older firmware it turns off at about 18 %.
- **Discharge Time Remaining** works with a margin as well, one that follows the load,
  see [Discharge Time Remaining](#discharge-time-remaining).

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.w150_xxxx_input_power
    to: "off"
    for: "00:03:00"
  - trigger: state
    entity_id: binary_sensor.w150_xxxx_shutdown_imminent
    to: "on"
actions:
  # replace with what shuts your equipment down, e.g. a shutdown button of its
  # integration
  - action: button.press
    target:
      entity_id: button.nas_shutdown
```

Use `to:` without `from:`. After a Bluetooth reconnect, and after a minute without
measurements, the binary sensors start from `unavailable`, so a trigger with
`from: "on"` misses an outage that begins at that moment.

## How much time Shutdown Imminent leaves

On battery, Shutdown Imminent turns on below 25 % of the whole charge, which is a Battery
Level of about 8.5 % with power-board firmware 1.29. The UPS turns its output off at its
reserve of 18 %, so Shutdown Imminent leaves about 7 % of the battery:

| UPS  | Power-board firmware | Energy left      | At 60 W         | At 100 W      |
|------|----------------------|------------------|-----------------|---------------|
| W150 | 1.29                 | about 3.3 Wh     | about 3 min     | under 2 min   |
| W150 | 1.21, 1.27           | about 3.7 Wh     | about 3.5 min   | about 2 min   |
| W180 | all                  | about 4.3–4.9 Wh | about 4–4.5 min | about 2.5 min |

The times assume a healthy battery and are estimated from the firmware. If an outage
starts while the battery is still below the reserve, for example a second outage soon
after the first, the UPS keeps its output on for only about 20 s.

## When the load is high

On battery, the UPS also turns its output off at a high load, whatever the charge, and
nothing reports this beforehand. It does so at once when Battery Current falls below
−13.8 A, and after a delay when it stays below about −11 A:

| Battery Current | Adapter Voltage above 16 V | 13–16 V      | 13 V or less  |
|-----------------|----------------------------|--------------|---------------|
| −12 A           | about 3 min                | about 6 min  | about 12 min  |
| −13 A           | about 1 min                | about 2 min  | about 4 min   |

The delay adds up over the whole outage and recovers only while the current stays above
about −6 A. As a rough guide, this starts at about 125–140 W output on a W150 and
140–160 W on a W180. Overload turns on above 120 W on battery, before that. If your load
can draw more than 120 W, start the shutdown as soon as Input Power turns off.

## Discharge Time Remaining

Discharge Time Remaining is the time until the UPS turns its output off at its reserve,
at the present load. The UPS estimates the time until its battery would be empty, which
its display shows, and the integration scales that down to the charge above the
reserve. At 50 W on a W150, it reads 3 to 4 minutes when Shutdown Imminent turns on,
where the display still shows about 15.

Treat it as the most time that is left:

- It assumes the reserve of 18 % the UPS keeps by default. Neither the vendor app nor
  this integration changes the reserve.
- It follows an average of the output current, so it takes a while to catch up after
  the load rises.
- A high load turns the output off earlier, see [When the load is high](#when-the-load-is-high),
  and so do an overheating battery and, at a very low load, Sleep Time.
- It is unknown when the UPS did not get its power board's versions, see
  [Versions](entities.md#versions).

## Power Event

Power Event fires *Power Lost* when the front panel sees the input more than 2 V below
the output, and *Power Restored* when that ends. This test is separate from Input Power:
an input that drops below the power-good threshold but stays within 2 V of the output
puts the UPS on battery without a Power Event. Use Input Power or Discharging to detect
outages.

Home Assistant restores the event's last state each time the UPS reconnects, so a state
trigger without conditions fires again. Use `not_from: unavailable`:

```yaml
triggers:
  - trigger: state
    entity_id: event.w150_xxxx_power_event
    not_from: unavailable
conditions:
  - condition: state
    entity_id: event.w150_xxxx_power_event
    attribute: event_type
    state: power_lost
```

## Sleep settings

Sleep Time and Sleep Min Current can turn the output off during an outage before a
shutdown has finished. On battery, the UPS sleeps once the output current stays below
Sleep Min Current for Sleep Time, and it does not wake up when the load rises again. The
vendor app recommends a Sleep Time of at least 60 s. Keep Sleep Min Current below the
lowest current your equipment draws, also when it idles.
