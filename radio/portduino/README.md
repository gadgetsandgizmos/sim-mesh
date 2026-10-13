# simradio for a Portduino firmware

A PlatformIO library that puts sim-mesh's simulated SX1262 (`radio/`) under a
firmware built on Portduino, Meshtastic's Arduino API for Linux, in place of
`/dev/spidev` and libgpiod. The firmware's radio driver is unchanged: it
talks SPI and pins as on a board, and each transfer reaches the chip model
whole.

```
firmware ── SPI.transfer(frame) ─────► SimChip ── simradio_transfer ──► chip model
firmware ── digitalWrite(RESET) ─────► ResetPin: rising edge ── simradio_reset
firmware ◄── digitalRead(BUSY/DIO1) ── LevelPin ◄── on_pin ── chip model
firmware ── rnode_idle(max_ms) ──────► poll() until max_ms, DIO1 rises, rnode_wake(), or a watched socket is readable (node time)
firmware ── listen/accept/close ─────► the socket watched while it is open (with -Wl,--wrap=…)
```

## What a firmware calls

Three functions, each a weak function of the firmware's own whose strong
definition is here:

- `void native_radio_backend_init()`, once, after the firmware knows its
  pins and before its `SPI.begin()` and its first touch of the radio. It
  opens the station's link to the ether (`SIM_MESH_NODE_ID`,
  `SIM_MESH_BIND_ADDR`, `SIM_MESH_ETHER`) and chip slot 0, installs its
  `SPIChip` into the global `SPI`, and binds the chip's lines with `gpioBind`.
- `void rnode_idle(uint32_t max_ms)`, where the firmware has nothing to do
  for up to `max_ms`. It is a `poll()` on an eventfd and the watched
  descriptors (below), until then, until DIO1 rises, or until one of them is
  readable; in a virtual-time run the time shim answers the wait in node
  time, and counts the descriptors it waits on, so a station with bytes
  waiting for it is never idle. Portduino's next `gpioIdle()` runs the DIO1
  interrupt handler. `max_ms == 0` returns at once.
- `void rnode_wake()`, from any thread that has work for the firmware's loop
  (a reader thread that took console input, say): the idle in progress
  returns at once, or the next one does.

and, declared by a firmware that uses them:

- `void rnode_idle_radio(uint32_t max_ms)`: the same wait without the
  watched descriptors, for a wait inside a radio operation (RadioLib's
  `yield()` during channel activity detection) that input must not end.
- `void rnode_watch_fd(int)`, `void rnode_unwatch_fd(int)`: a descriptor
  whose readability ends `rnode_idle`, at most 16.
- `int rnode_idle_fd_ready()`: whether the last `rnode_idle` ended on a
  watched descriptor; asking clears it. A firmware that polls its sockets on
  timers makes them due at once when it is set.

A watched descriptor whose peer has gone (`POLLRDHUP`, `POLLHUP`, `POLLERR`
or `POLLNVAL`) is reported ready once and then no longer watched: at end of
stream it is readable for ever, and a firmware that never notices (Portduino's
`WiFiClient` does not) would end every idle at once and T would not move.
The idle never reads a watched descriptor to find this out, since the shim
counts what a station takes from a connection.

**The link wraps.** A firmware linked with `-Wl,--wrap=bind
-Wl,--wrap=listen -Wl,--wrap=accept -Wl,--wrap=close` gets them from here:
a bind of an IPv4 socket to `INADDR_ANY` binds `SIM_MESH_BIND_ADDR` instead,
when that is set, so every station keeps its port on its own address; a
socket that `listen` or `accept` succeeds on is watched; `close` unwatches.
Their `__real_*` are weak, so a firmware linked without the wraps links this
library too. The idle and the wraps are `idle.cpp`, POSIX alone, which
`radio/tests/test_portduino_idle.py` compiles with a stand-in on the host's
g++.

The firmware must hand every SPI transaction to `SPI.transfer` as one frame,
NSS low to NSS high, which is what a driver written for spidev already does.
Binding a pin also turns off Portduino's 100 ms sleep between `loop()` calls,
so the firmware's own idle is the only wait in its loop.

## Its pins

The chip's four lines are bound at `SIMRADIO_PIN_NSS`, `SIMRADIO_PIN_RESET`,
`SIMRADIO_PIN_BUSY` and `SIMRADIO_PIN_DIO1` (1, 2, 3 and 4 when unset), which
must be the pins the firmware drives the chip on. NSS does nothing, since
each frame is already one transfer.

Portduino finds an interrupt's edge by reading the pin once a loop, so a
firmware that idles between loops could miss DIO1 going low and high again
(one reception cleared, the next arrived) and with it the interrupt. DIO1
never loses a rise: when the line rose since Portduino's last read and that
read saw it high, it reads low once and the idle in progress ends, so the
next loop's read sees it rise.

## Building with it

`sim` fetches or builds `radio/build/libsimradio-sx1262.so` as it starts, which
`link.py` links the program with by name; the firmware's zip does not carry
it, since sim-mesh provides it when it starts the station. A firmware's
environment lists this library with a `symlink://` entry in `lib_deps`.
`casefold.py`, as a `pre:` extra script, lets Portduino build on a
case-insensitive filesystem, where its `String.h` would otherwise answer
`#include <string.h>`. This library is linked as objects, not as an archive,
so its strong definitions win over the firmware's weak ones.

**The other architecture.** Pre-built firmware comes for aarch64 and x86_64,
so a firmware builds both, the one that is not the machine's with that
architecture's cross g++ (`<arch>-linux-gnu-g++`) and its libc from the
multiarch packages. `SIM_MESH_ARCH=<arch>` in the build's environment names
it; `cross.py`, as a `post:` extra script, swaps the cross tools into the
program's, the sources' and every library's environment, and `link.py`
compiles the radio with them into `radio/build.linux-<arch>/` and links that
copy instead. Each architecture wants a build directory of its own
(`PLATFORMIO_BUILD_DIR`).
