# Entities

The UPS sends its measurements about once a second; the integration reads its settings
each time it connects. Entity names follow the vendor app's wording in English,
简体中文 and Русский.

When no measurement arrives for a minute, for example while the UPS has turned its
output off on battery or updates its firmware, the measured values and status sensors
show as unavailable until measurements resume. The settings stay available.

## Battery

- **Battery Level**: with power-board firmware 1.29 it counts only the charge above the
  reserve the UPS keeps, 18 % by default, so 0 % is where the UPS turns its output off.
  With older firmware it counts the whole charge, and the output turns off at about 18 %.
- **Battery Voltage**: the sum of the cell voltages, corrected for the battery's internal
  resistance.
- **Battery Current**: positive while the battery charges, negative while it discharges.
- **Cell 1–4 Voltage**: the four cells in series, from the bottom of the stack.
- **Max Voltage Difference**: the highest minus the lowest cell voltage, the vendor
  app's balance indicator.
- **Battery Health**: estimated from Number of Cycles the way the vendor cloud does for
  the W150, from 100 % up to 100 cycles down to 0 % at 1500. The UPS reports no health
  value, and the cloud's estimate for the W180 is not known.
- **Number of Cycles**: equivalent full cycles, the charge moved into and out of the
  battery divided by twice its capacity. A factory reset sets it to 0.
- **Battery Temperature**: the battery temperature the UPS measures.

## Input and output

- **DC Input Voltage**, **DC Output Voltage** and **DC Output Current** are measured.
- **DC Input Current**: estimated by the UPS from the output current and the charging
  power, not measured.
- **Output Power**: DC Output Voltage times DC Output Current.
- **Total Energy Consumed**: the energy delivered at the output. The UPS saves it every
  2 Wh, so it can drop by up to 2 Wh when the UPS's power board restarts.
- **Discharge Time Remaining**: reported only while the output runs on battery. Like the
  UPS display, it counts down to an empty battery, but the UPS turns its output off
  earlier, at its reserve; see [Discharge Time Remaining](shutdown.md#discharge-time-remaining).

## Status

- **Input Power**: on while the input voltage is above the power-good threshold, by
  default 95.8 % of Adapter Voltage. Below it, the UPS runs from the battery.
- **Charging**: the battery is charging.
- **Discharging**: the output runs from the battery. It turns on when input power is lost
  and stays on for about 30 s after it returns.
- **Overload**: the output draws more than 120 W on battery. The UPS only reports it, but
  a higher load makes it turn its output off, see
  [When the load is high](shutdown.md#when-the-load-is-high).
- **Shutdown Imminent**: on battery, the charge is below 25 % of the whole charge, which
  is a Battery Level of about 8.5 % with power-board firmware 1.29. The UPS turns its
  output off a few minutes later, see
  [How much time Shutdown Imminent leaves](shutdown.md#how-much-time-shutdown-imminent-leaves).
- **Battery Fault**: a cell above its maximum voltage, or a discharge over-current or
  short circuit while the UPS runs from input power. Other battery faults don't show
  here: on battery the UPS shuts down instead, and a failing battery monitor makes the
  UPS restart.
- **Battery Overheating**: the battery is above 60 °C on the W150 or 50 °C on the W180.
- **Battery Too Cold**: the battery is below −10 °C.
- **Input Overvoltage**: on while input power is present and DC Output Voltage, which
  then follows the input, is at least 2 V above Adapter Voltage: the adapter delivers
  more than Adapter Voltage says. The power board's own over-voltage flag also turns it
  on, but the power board rarely sets it: only when the voltage passes about 1.8 V above
  Adapter Voltage at the moment input power changes.
- **Output Overcurrent**: the output current is above the UPS's limit, 155 W (W150) or
  185 W (W180) divided by Adapter Voltage, at most 11.5 A or 12.5 A. The UPS only
  reports it.
- **Wi-Fi**: the UPS's own network connection, which the vendor cloud and Wake-on-LAN
  use. It is updated every minute, together with Wi-Fi Signal, Wi-Fi Network and IP
  Address.

## Power Event

**Power Event** fires *Power Lost* when the front panel sees the input more than 2 V
below the output and *Power Restored* when that ends. See
[Power Event](shutdown.md#power-event) before using it in automations.

## Versions

The device page shows the firmware and hardware versions of the UPS's front panel,
**UPS Firmware Version** and **UPS Hardware Version** those of its power board, as the
numbers the UPS reports. The vendor app shows them in its own format, e.g. firmware 19
as V1.19. The power board's hardware version tells the model: 3 is a W150, 4 a W180.
Without the power board's versions, the model shows as WalleCube UPS, see
[Controls](#controls).

## Controls

The controls mirror the vendor app's advanced configuration page.

The UPS asks its power board once for its versions and the adapter and sleep settings,
shortly after it starts. When the integration connects before that, it reads them again
a few seconds later. If the UPS did not get them, they show as unknown, and the adapter
and sleep settings can't be changed until the UPS restarts.

- **Adapter Voltage** and **Adapter Current** (disabled by default) must match the label
  of the power adapter feeding the UPS. The UPS also generates Adapter Voltage at its
  output while on battery, and derives the power-good threshold and its other adapter
  limits from the two values, like the app. Wrong values can stop the battery from
  charging or make the UPS treat a working adapter as lost. A change fails with an error
  if the UPS does not confirm that its power board received it. The power board applies
  it when it restarts, so gently press the reset hole on the front panel afterwards, as
  the app asks; only the charging-current limit follows a new Adapter Current at once.
- **Sleep Time** and **Sleep Min Current**: on battery, the UPS turns its output off once
  the load stays below Sleep Min Current for Sleep Time. It wakes up when input power
  returns or the front-panel button is pressed, not when the load rises again. The vendor
  app recommends a Sleep Time of at least 60 s, see [Sleep settings](shutdown.md#sleep-settings).
  Power boards older than 1.29 accept at most 2000 mA.
- **Screen Timeout** and **Keep Screen On**: when the screen switches to its idle view.
- **Screen Brightness** and **Screen Idle Brightness** are not in the vendor app. The idle
  brightness applies once the screen timeout expires. Screen Brightness goes up to 80 %,
  the most the UPS allows, and at 0 % the screen stays faintly lit. On front-panel build
  v1.0-29, one of the two builds of firmware 1.17, the idle brightness is fixed and
  Screen Idle Brightness has no effect.
- **Temperature Unit**: the unit on the UPS's screen. It needs front-panel firmware 1.18
  or newer and is left out on older firmware.
- **Screen Language**: English or Chinese on the UPS's screen.
- **Buzzer**: *Mute*, *Beep Once* when input power is lost or returns, or *Repeat*, which
  beeps every 2.5 s while input power is lost.
