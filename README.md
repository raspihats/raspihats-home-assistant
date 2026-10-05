# Raspihats for Home Assistant

Control [Raspihats](https://raspihats.com) I2C-HAT relay and input boards from Home Assistant, set up entirely from the UI.

Relays appear as switches and inputs as binary sensors. The edge counters on the input boards appear as sensors. Input boards signal changes on their interrupt line, so Home Assistant sees every edge at once, including pulses shorter than a poll. The settings that make the boards fail safe are configured from Home Assistant and stored on the board itself:

- **Communication watchdog**: when the board stops hearing from Home Assistant (a crash, a hang, a cable pulled), it switches its outputs to a defined safe state on its own, without waiting for Home Assistant to come back.
- **Safe state per output**: choose which outputs switch on, switch off or keep their state when the watchdog trips.
- **Power-on state**: the state the outputs take when the board powers up, before Home Assistant is running.
- **Input inversion**, applied by the board before debouncing, so the edge counters follow the inverted signal too.

The board firmware keeps the watchdog, the safe state and the power-on state, so they work while Home Assistant is restarting, updating or down.

## Supported boards

| Board | Channels | Address range |
|---|---|---|
| [DI16ac I2C-HAT](https://raspihats.com/shop/di16ac-i2c-hat/) | 16 isolated inputs | 0x40–0x4F |
| [DI6acDQ6rly I2C-HAT](https://raspihats.com/shop/di6acdq6rly-i2c-hat/) | 6 isolated inputs, 6 relays | 0x60–0x6F |
| [DQ10rly I2C-HAT](https://raspihats.com/shop/dq10rly-i2c-hat/) | 10 relays | 0x50–0x5F |
| [DQ5rly I2C-HAT](https://raspihats.com/shop/dq5rly-i2c-hat/) | 5 relays | 0x50–0x5F |

Boards can be stacked; each is added to Home Assistant separately, at the address set by its jumpers.

## Requirements

Home Assistant has to run **on the Raspberry Pi the boards are mounted on**, because the integration talks to the I2C bus directly. Any installation type works (Home Assistant OS, Container, Core) as long as `/dev/i2c-1` is available to it:

- **Home Assistant OS**: I2C is off by default. Enable it as described in the [Home Assistant OS documentation for the Raspberry Pi](https://developers.home-assistant.io/docs/operating-system/boards/raspberrypi/#i2c) (a `dtparam` line in `config.txt` plus an `i2c-dev` module file, via a USB stick named CONFIG), or with the community add-on *HassOS I2C Configurator*. Reboot twice: the first boot imports the configuration, the second loads it.
- **Container**: enable I2C on the host (`sudo raspi-config nonint do_i2c 0` on Raspberry Pi OS) and pass the devices to the container: `--device /dev/i2c-1`, plus the header GPIO controller for the interrupt line (`--device /dev/gpiochip0`; check with `gpioinfo` which chip is labelled `pinctrl-…`).
- **Core**: enable I2C on the host and add the user running Home Assistant to the `i2c` and `gpio` groups.

## Installation

With [HACS](https://hacs.xyz):

1. In HACS, open the menu (⋮) and choose **Custom repositories**.
2. Add `https://github.com/raspihats/raspihats-home-assistant` with the type **Integration**.
3. Find **Raspihats** in HACS, download it, and restart Home Assistant.

Manually: copy `custom_components/raspihats` into the `custom_components` folder of your Home Assistant configuration, then restart.

## Adding a board

**Settings → Devices & services → Add integration → Raspihats**. Pick the board model, then its address. The integration reads the board's name before adding it, so a wrong address or model is reported instead of being added. For an input board it then asks for the interrupt line (see [Interrupts](#interrupts)).

Each output and input is named after its label on the board (`Q0`, `I3`…). Rename the entities as you like; for inputs, **Show as** in the entity settings sets what they represent (door, motion, …).

The edge counters (a rising and a falling one per input) are disabled by default; enable the ones you need. The board counts every edge, including pulses too short for a poll to see. The counters restart from 0 when the board loses power, which Home Assistant's statistics treat as a meter reset.

## Board settings

Open the board's entry and choose **Configure**. On first open, the form shows the values the board holds, so saving it unchanged writes nothing.

| Setting | Stored on | Notes |
|---|---|---|
| Polling interval | Home Assistant | Default 250 ms. Every poll also feeds the board's watchdog. |
| Watchdog timeout | Board | 0 turns it off. Must cover at least four polls, and 2 s minimum. On input boards a trip stops edge capture until Home Assistant re-arms it. |
| Outputs switched ON when the watchdog trips | Board | The rest switch OFF, except those set to keep their state. |
| Outputs that keep their state when the watchdog trips | Board | |
| Outputs switched ON at power-up | Board | The rest start OFF. |
| Inverted inputs | Board | |
| Interrupt line | Home Assistant | Input boards; see [Interrupts](#interrupts). |

Board settings are written only when they differ from what the board holds, since they live in its EEPROM. A board that arrives with a watchdog shorter than the polling allows (left by a test rig, say) is reported under **Settings → Repairs** and polled fast enough to keep it fed, until you set its watchdog timeout. They are checked again when Home Assistant starts and when a board restarts, so a replacement board gets the same configuration.

**Choose the watchdog timeout with Home Assistant restarts in mind.** A restart or update that takes longer than the timeout trips the watchdog, and the outputs go to their safe state until Home Assistant is back. For most installations that is the point: what the safe state is for. For loads that should ride through a restart, mark those outputs to keep their state, or use a timeout longer than a restart takes.

## Interrupts

The DI16ac and DI6acDQ6rly record every input edge in a capture queue on the board, together with the state of all inputs at that moment, and pull an interrupt line low while edges are waiting. With the interrupt line set, Home Assistant reads the queue as soon as the line goes low and replays the edges in order. An input change shows up within a few tens of milliseconds, and a pulse much shorter than the polling interval still arrives as *on* then *off*, so automations triggered by it run.

- The interrupt jumper on the board selects the GPIO: **GPIO21** from the factory, or GPIO20, 22 or 23. Set the same GPIO in Home Assistant. Several input boards on one stack can share a GPIO.
- If the GPIO is not available to Home Assistant, the integration logs a warning and reads the capture queue at every poll instead: no edge is lost, changes just arrive at the polling interval.
- **Off** leaves the capture queue unarmed and polls the inputs only. Use it if the GPIO is needed by something else, since an armed board drives it. With Off, a pulse shorter than the polling interval can be missed (the edge counters still count it).
- Firmware 2.x works too. There the arming is not stored on the board, so the integration arms it again after every board restart, and an input read releases the line; an edge that arrives during a poll is then picked up by that poll's own read of the queue rather than by the interrupt. Boards whose firmware has no capture queue at all are not offered the interrupt line, and their inputs are polled.

The board disarms its capture queue when it restarts (firmware 3.x also when its watchdog trips); the integration re-arms it at the next poll. Removing the board from Home Assistant disarms it, so no unserved board holds the line low.

## Coming from the old Raspihats integration

Home Assistant's built-in `raspihats` integration (YAML configuration under `switch:` and `binary_sensor:`) was removed when Home Assistant stopped accepting integrations that access GPIO and I2C directly. This integration replaces it for the current boards. Remove the old YAML, then add the boards from the UI:

| Old YAML option | Now |
|---|---|
| `name` | Rename the entity |
| `device_class` | **Show as** in the entity settings |
| `invert_logic` (inputs) | **Inverted inputs** in the board settings |
| `initial_state` (outputs) | **Outputs switched ON at power-up** in the board settings |

## Troubleshooting

- **"The I2C bus /dev/i2c-1 is not available"**: I2C is not enabled, or the device is not passed to Home Assistant; see [Requirements](#requirements).
- **"No board answered at this address"**: check the address jumpers and that the board is seated on the header. `i2cdetect -y 1` on the host lists the addresses that answer.
- **"The interrupt line GPIO21 is not available"** in the log: the GPIO controller is not accessible to Home Assistant (see [Requirements](#requirements)), or another program holds the pin. Inputs keep working through polling.
- **"GPIO21 stays low after draining every board on it"**: something other than the boards set up here is pulling the line low, such as an input board armed by another program, or a different HAT using the same pin. Move one of them to another GPIO.
- **A board added from the device list does not show up in that list**: the device list opened from the Raspihats card (*N devices* → **Add device**) does not refresh after the setup flow finishes, so the new board appears only after reloading the page. Boards added from the Raspihats integration page show up straight away. This is in Home Assistant's frontend, not in this integration: the list loads the integration's entries once, when the page opens, and filters devices by them.
- **Diagnostics**: the board's entry has **Download diagnostics**, with the board model, firmware, stored settings and last readings. Attach it to an [issue](https://github.com/raspihats/raspihats-home-assistant/issues).

## License

MIT
