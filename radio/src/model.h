/**
 * model — the SX1262 behind the C ABI.
 *
 * The driver above it is the one that runs on hardware, unchanged: it writes
 * the same opcodes, reads the same registers, polls or waits on the same DIO1
 * line and reads the same IRQ bits. What this supplies is the other end of the
 * bus — a command interpreter, a register file, a payload buffer, and the
 * timing of a frame, which is where a radio actually lives.
 *
 * A frame in flight is three instants: the end of its preamble, the end of its
 * header, and the end of the frame. The model schedules the receiver's
 * interrupts on those instants with one-shot timers, so a driver that disables
 * its interrupt, drains, and re-enables sees exactly the edges it sees on a
 * board.
 *
 * Commands take effect at once (BUSY is never busy unless SIM_MESH_BOARD gives
 * the board's figures, `busyFigures` in model.cpp), with one exception the
 * part imposes itself: with DIO3 driving a TCXO (SetDIO3AsTCXOCtrl), leaving
 * STDBY_RC or SLEEP for a mode that runs on the reference waits out the
 * start-up the driver programmed. A frame goes on the air, and a receiver or a
 * CAD starts, only then; meanwhile the medium sees the chip as FS. STDBY_XOSC,
 * FS and a fallback to either keep the reference running, as on the part.
 *
 * What is between two radios is the ether (ether_link.cpp): the model hands it
 * every transmission and every change of mode or carrier, and is handed back
 * the frames that reach its antenna.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

/** A frame arriving at this receiver: when its stages land, relative to the
 *  `t0` the sender stamped, and how strongly it arrives. `energyOnly` is the
 *  ether's `"cad": true`: the frame is in the air at this antenna and the
 *  demodulator is not to follow it. `hdrOk` false is the ether's `"hdr_ok":
 *  false`: the header fails at `tHdr`, and the chip lets go of the frame.
 *  `det` is the ether's `"det"`: which side detector found the frame, 0 the
 *  main one; a chip with none only ever sees 0. */
struct VirtualRxBegin {
    int     id;
    int64_t t0, tPre, tHdr, tEnd;   /* the sender's own microsecond stamps */
    int     levelDbm;
    bool    energyOnly;
    bool    hdrOk;
    int     det;
};

/** The same frame, finished: what it carried and how it came out. */
struct VirtualRxEnd {
    int            id;
    const uint8_t* payload;
    size_t         len;
    bool           crcOk;       /* false when the ether's verdict is not clean */
    bool           hdrOk;       /* false for a "hdr" verdict: the header's fate, when
                                  * the ether only knows it this late (rx_begin's own
                                  * hdrOk, true here by default, said so already) */
    int            rssiDbm;
    int            snrDb;
};

/** The noise floor a receiver reports when nothing is arriving. */
constexpr int kNoiseFloorDbm = -110;

/** The most radio slots a station has: chips 0 to 7. */
constexpr int kMaxSlots = 8;

/** The chip for a slot, if one has been opened. */
struct simradio* modelChip(int slot);

void modelRxBegin(struct simradio* chip, const VirtualRxBegin& f);
void modelRxEnd(struct simradio* chip, const VirtualRxEnd& f);

/** While a datagram from the ether is applied, DIO1's changes are kept, not
 *  told; release tells them, in the order they came, before the host's waits
 *  run (conductor::release), so no host thread sees part of an instant. */
void modelHoldPins();
void modelReleasePins();
